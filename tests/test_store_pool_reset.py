from datetime import date

import pytest
from pydantic import ValidationError

from app.api import schemas
from app.db import models
from app.api.schemas import BrowserStoreProduct, HistoricalSalesPoint
from app.db.models import (
    CollectionAttempt,
    Product,
    ProductDailyMetric,
    ProductReview,
    ProductSnapshot,
    Store,
    StoreDailyMetric,
    StorePoolResetBatch,
    StorePoolResetStore,
)


def test_reset_batch_and_store_checkpoint_persist_frozen_window_and_progress(db_session):
    assert hasattr(models, "StorePoolResetBatch")
    assert hasattr(models, "StorePoolResetStore")
    batch_model = models.StorePoolResetBatch
    task_model = models.StorePoolResetStore

    store = Store(
        aliexpress_store_id="reset-store-1",
        name="Reset Store",
        url="https://www.aliexpress.com/store/1100000001",
        status="active",
    )
    batch = batch_model(
        target_date=date(2026, 10, 16),
        window_start=date(2026, 10, 9),
        window_end=date(2026, 10, 15),
        status="PENDING",
        dispatch_enabled=False,
    )
    task = task_model(
        batch=batch,
        store=store,
        platform_store_id="1100000001",
        store_name="Reset Store",
        orders_url="https://www.aliexpress.com/store/1100000001/pages/all-items.html?shop_sortType=orders_desc",
        position=1,
        status="PENDING",
        visited_page_urls=["https://www.aliexpress.com/store/1100000001/pages/all-items.html?shop_sortType=orders_desc"],
        staged_candidates=[{"platform_product_id": "10050001", "orders_rank": 1}],
        diagnostics=[{"reason": "chart_missing", "product_id": "10050002"}],
        scanned_candidate_count=1,
        scanned_page_count=1,
    )
    db_session.add(task)
    db_session.commit()

    loaded = db_session.get(batch_model, batch.id)
    loaded_task = db_session.get(task_model, task.id)
    assert (loaded.target_date, loaded.window_start, loaded.window_end) == (
        date(2026, 10, 16),
        date(2026, 10, 9),
        date(2026, 10, 15),
    )
    assert loaded.status == "PENDING"
    assert loaded.dispatch_enabled is False
    assert loaded_task.staged_candidates == [{"platform_product_id": "10050001", "orders_rank": 1}]
    assert loaded_task.diagnostics == [{"reason": "chart_missing", "product_id": "10050002"}]
    assert loaded_task.visited_page_urls == [task.orders_url]


def test_reset_create_schema_requires_explicit_replace_confirmation():
    request = schemas.StorePoolResetCreateRequest
    with pytest.raises(ValidationError):
        request.model_validate({"confirm_replace": False})

    confirmed = request.model_validate({"confirm_replace": True})
    assert confirmed.confirm_replace is True


def test_legacy_browser_worker_capabilities_default_to_empty(db_session):
    from app.db.models import BrowserCollectionWorker

    assert "capabilities" in BrowserCollectionWorker.__table__.columns
    worker = BrowserCollectionWorker(
        worker_id="old-extension",
        label="Old extension",
        extension_version="0.6.80",
        last_heartbeat_at=models.utcnow(),
    )
    db_session.add(worker)
    db_session.commit()
    db_session.refresh(worker)

    assert worker.capabilities == []


def _reset_candidate(product_id, orders_rank, values, *, source="ixspy_visible_daily_increment", dates=None):
    from app.services.store_pool_reset import RankedPoolResetCandidate

    window_dates = dates or [date(2026, 10, day) for day in range(9, 16)]
    product = BrowserStoreProduct(
        platform_product_id=product_id,
        url=f"https://www.aliexpress.com/item/{product_id}.html",
        title=f"Candidate {product_id}",
        historical_sales=[
            HistoricalSalesPoint(date=day, value=value, value_type="daily_increment")
            for day, value in zip(window_dates, values)
        ],
        history_source=source,
        history_daily_capture_complete=True,
    )
    return RankedPoolResetCandidate(
        product=product,
        orders_rank=orders_rank,
        page_url="https://www.aliexpress.com/store/1100000001/pages/all-items.html?shop_sortType=orders_desc",
    )


def test_pool_reset_ranking_uses_increment_sum_then_orders_rank_then_product_id_and_caps_at_20():
    from app.services.store_pool_reset import rank_pool_reset_candidates

    candidates = [_reset_candidate("p-score", 50, [100, 0, 0, 0, 0, 0, 0])]
    candidates.extend([
        _reset_candidate("p-b", 2, [1] * 7),
        _reset_candidate("p-a", 2, [1] * 7),
    ])
    candidates.extend(
        _reset_candidate(f"p-{rank:03}", rank, [1] * 7)
        for rank in range(3, 23)
    )

    ranked, diagnostics = rank_pool_reset_candidates(
        candidates, date(2026, 10, 9), date(2026, 10, 15)
    )

    assert len(ranked) == 20
    assert [item.product.platform_product_id for item in ranked[:4]] == [
        "p-score", "p-a", "p-b", "p-003"
    ]
    assert ranked[-1].orders_rank == 19
    assert [item["reason"] for item in diagnostics].count("rank_limit_exceeded") == 3


@pytest.mark.parametrize(
    ("values", "source", "dates", "reason"),
    [
        ([2, 3, 4, 5, 6, 7], "ixspy_visible_daily_increment", None, "seven_day_history_incomplete"),
        ([2000, 2001, 2002, 2003, 2004, 2005, 2006], "ixspy_visible_daily_increment", None, "cumulative_like_history"),
        ([1] * 7, "unknown_source", None, "history_source_untrusted"),
        ([1] * 7, "ixspy_visible_daily_increment", [date(2026, 10, day) for day in [9, 10, 11, 12, 13, 14, 14]], "seven_day_history_incomplete"),
    ],
)
def test_pool_reset_ranking_excludes_untrusted_or_incomplete_history(values, source, dates, reason):
    from app.services.store_pool_reset import rank_pool_reset_candidates

    ranked, diagnostics = rank_pool_reset_candidates(
        [_reset_candidate("p-invalid", 1, values, source=source, dates=dates)],
        date(2026, 10, 9),
        date(2026, 10, 15),
    )

    assert ranked == []
    assert diagnostics == [{"product_id": "p-invalid", "orders_rank": 1, "reason": reason}]


def test_pool_reset_ranking_keeps_first_duplicate_product_and_reports_later_occurrence():
    from app.services.store_pool_reset import rank_pool_reset_candidates

    first = _reset_candidate("p-duplicate", 8, [1] * 7)
    duplicate = _reset_candidate("p-duplicate", 1, [100] * 7)

    ranked, diagnostics = rank_pool_reset_candidates(
        [first, duplicate], date(2026, 10, 9), date(2026, 10, 15)
    )

    assert len(ranked) == 1
    assert ranked[0].orders_rank == 8
    assert diagnostics == [{
        "product_id": "p-duplicate",
        "orders_rank": 1,
        "reason": "duplicate_product_id",
    }]


def test_pool_reset_replacement_preserves_history_and_reactivates_selected_rows(db_session):
    store = Store(
        aliexpress_store_id="1100000010",
        name="Reset Store",
        url="https://www.aliexpress.com/store/1100000010",
        status="active",
    )
    batch = StorePoolResetBatch(
        target_date=date(2026, 10, 16),
        window_start=date(2026, 10, 9),
        window_end=date(2026, 10, 15),
        status="RUNNING",
        dispatch_enabled=True,
    )
    db_session.add_all([store, batch])
    db_session.flush()
    retained = Product(
        store_id=store.id, aliexpress_product_id="100500000001",
        url="https://www.aliexpress.com/item/100500000001.html", status="active",
    )
    removed = Product(
        store_id=store.id, aliexpress_product_id="100500000002",
        url="https://www.aliexpress.com/item/100500000002.html", status="active",
    )
    returning = Product(
        store_id=store.id, aliexpress_product_id="100500000003",
        url="https://www.aliexpress.com/item/100500000003.html", status="inactive",
    )
    db_session.add_all([retained, removed, returning])
    db_session.flush()
    historical_snapshot = ProductSnapshot(
        product_id=removed.id,
        collector_name="prior-collector",
        collector_version="1",
        parse_status="success",
        source="MANUAL",
        status="VALID",
        raw_data={"kept": True},
    )
    historical_metric = ProductDailyMetric(
        product_id=removed.id,
        metric_date=date(2026, 10, 8),
        estimated_sales=77,
        is_estimable=True,
        reason="prior observation",
        calculation_method="ixspy_visible_daily_increment",
    )
    historical_attempt = CollectionAttempt(
        product_id=removed.id,
        source="MANUAL",
        status="VALID",
    )
    historical_review = ProductReview(
        product_id=removed.id,
        identity_hash="prior-review-hash",
        review_text="Keep this review",
    )
    db_session.add_all([
        historical_snapshot, historical_metric, historical_attempt, historical_review,
    ])
    task = StorePoolResetStore(
        batch=batch,
        store=store,
        platform_store_id=store.aliexpress_store_id,
        store_name=store.name,
        orders_url="https://www.aliexpress.com/store/1100000010/pages/all-items.html?shop_sortType=orders_desc",
        position=1,
        status="RUNNING",
    )
    db_session.add(task)
    db_session.flush()

    selected = [
        _reset_candidate("100500000001", 1, [1] * 7),
        _reset_candidate("100500000003", 2, [2] * 7),
        _reset_candidate("100500000004", 3, [3] * 7),
    ]
    from app.services.store_pool_reset import replace_store_pool_for_store

    result = replace_store_pool_for_store(db_session, batch, task, selected)
    db_session.flush()

    active_ids = set(db_session.query(Product.aliexpress_product_id).filter_by(
        store_id=store.id, status="active"
    ).all())
    assert active_ids == {("100500000001",), ("100500000003",), ("100500000004",)}
    assert db_session.get(Product, removed.id).status == "inactive"
    assert db_session.get(Product, returning.id).status == "active"
    assert db_session.query(ProductSnapshot).filter_by(id=historical_snapshot.id).count() == 1
    assert db_session.query(ProductDailyMetric).filter_by(id=historical_metric.id).count() == 1
    assert db_session.query(CollectionAttempt).filter_by(id=historical_attempt.id).count() == 1
    assert db_session.query(ProductReview).filter_by(id=historical_review.id).count() == 1
    assert db_session.query(ProductDailyMetric).filter(
        ProductDailyMetric.metric_date >= date(2026, 10, 9),
        ProductDailyMetric.metric_date <= date(2026, 10, 15),
    ).count() == 21
    assert db_session.query(StoreDailyMetric).filter_by(store_id=store.id).count() == 7
    assert result["selected_count"] == 3
    assert result["deactivated_ids"] == ["100500000002"]


def test_pool_reset_replacement_does_not_change_pool_of_deactivated_store(db_session):
    store = Store(
        aliexpress_store_id="1100000011",
        name="Inactive Store",
        url="https://www.aliexpress.com/store/1100000011",
        status="inactive",
    )
    batch = StorePoolResetBatch(
        target_date=date(2026, 10, 16),
        window_start=date(2026, 10, 9),
        window_end=date(2026, 10, 15),
        status="RUNNING",
        dispatch_enabled=True,
    )
    db_session.add_all([store, batch])
    db_session.flush()
    product = Product(
        store_id=store.id, aliexpress_product_id="100500000011",
        url="https://www.aliexpress.com/item/100500000011.html", status="active",
    )
    task = StorePoolResetStore(
        batch=batch, store=store, platform_store_id=store.aliexpress_store_id,
        store_name=store.name,
        orders_url="https://www.aliexpress.com/store/1100000011/pages/all-items.html?shop_sortType=orders_desc",
        position=1, status="RUNNING",
    )
    db_session.add_all([product, task])
    db_session.flush()
    from app.services.store_pool_reset import replace_store_pool_for_store

    result = replace_store_pool_for_store(
        db_session, batch, task, [_reset_candidate("100500000012", 1, [1] * 7)]
    )

    assert result["skipped"] is True
    assert db_session.get(Product, product.id).status == "active"
    assert db_session.query(Product).filter_by(store_id=store.id).count() == 1


@pytest.mark.parametrize(
    ("foreign_id", "foreign_url", "expected_reason"),
    [
        (
            "100500000022",
            "https://www.aliexpress.com/item/100500000022.html",
            "product_id_owned_by_other_store",
        ),
        (
            "100500000099",
            "https://www.aliexpress.com/item/100500000022.html",
            "product_url_owned_by_other_store",
        ),
    ],
)
def test_pool_reset_replacement_excludes_candidate_id_or_url_owned_by_another_store(
    db_session, foreign_id, foreign_url, expected_reason
):
    store = Store(
        aliexpress_store_id="1100000020",
        name="Target Store",
        url="https://www.aliexpress.com/store/1100000020",
        status="active",
    )
    foreign_store = Store(
        aliexpress_store_id="1100000021",
        name="Foreign Store",
        url="https://www.aliexpress.com/store/1100000021",
        status="active",
    )
    batch = StorePoolResetBatch(
        target_date=date(2026, 10, 16),
        window_start=date(2026, 10, 9),
        window_end=date(2026, 10, 15),
        status="RUNNING",
        dispatch_enabled=True,
    )
    db_session.add_all([store, foreign_store, batch])
    db_session.flush()
    old_target_product = Product(
        store_id=store.id,
        aliexpress_product_id="100500000020",
        url="https://www.aliexpress.com/item/100500000020.html",
        status="active",
    )
    foreign_product = Product(
        store_id=foreign_store.id,
        aliexpress_product_id=foreign_id,
        url=foreign_url,
        status="active",
    )
    task = StorePoolResetStore(
        batch=batch,
        store=store,
        platform_store_id=store.aliexpress_store_id,
        store_name=store.name,
        orders_url="https://www.aliexpress.com/store/1100000020/pages/all-items.html?shop_sortType=orders_desc",
        position=1,
        status="RUNNING",
    )
    db_session.add_all([old_target_product, foreign_product, task])
    db_session.flush()

    from app.services.store_pool_reset import replace_store_pool_for_store

    result = replace_store_pool_for_store(
        db_session,
        batch,
        task,
        [
            _reset_candidate("100500000022", 1, [9] * 7),
            _reset_candidate("100500000023", 2, [8] * 7),
        ],
    )

    assert result["selected_ids"] == ["100500000023"]
    assert any(item["reason"] == expected_reason for item in result["diagnostics"])
    assert db_session.get(Product, foreign_product.id).store_id == foreign_store.id
    assert db_session.get(Product, old_target_product.id).status == "inactive"

