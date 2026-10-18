from datetime import date
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.api import schemas
from app.db import models
from app.api.schemas import BrowserStoreProduct, HistoricalSalesPoint
from app.db.models import (
    BrowserCollectionControl,
    BrowserCollectionItem,
    BrowserCollectionRun,
    BrowserCollectionWorker,
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


def _make_store(db_session, store_id, *, status="active"):
    store = Store(
        aliexpress_store_id=store_id,
        name=f"Store {store_id}",
        url=f"https://www.aliexpress.com/store/{store_id}",
        status=status,
    )
    db_session.add(store)
    db_session.flush()
    return store


def _held_reset_batch(db_session, monkeypatch, store_ids=("1100000030",)):
    from app.services import store_pool_reset

    monkeypatch.setattr(store_pool_reset, "_reset_target_date", lambda: date(2026, 10, 16))
    for store_id in store_ids:
        _make_store(db_session, store_id)
    return store_pool_reset.create_pool_reset_batch(db_session, confirm_replace=True)


def test_create_pool_reset_batch_snapshots_only_active_stores_and_freezes_seven_dates(
    db_session, monkeypatch
):
    from app.services.store_pool_reset import create_pool_reset_batch

    active_a = _make_store(db_session, "1100000030")
    active_b = _make_store(db_session, "1100000031")
    inactive = _make_store(db_session, "1100000032", status="inactive")
    inactive_product = Product(
        store_id=inactive.id,
        aliexpress_product_id="100500000032",
        url="https://www.aliexpress.com/item/100500000032.html",
        status="active",
    )
    db_session.add(inactive_product)
    db_session.flush()
    from app.services import store_pool_reset

    monkeypatch.setattr(store_pool_reset, "_reset_target_date", lambda: date(2026, 10, 16))
    batch = create_pool_reset_batch(db_session, confirm_replace=True)
    assert batch.status == "PENDING"
    assert batch.dispatch_enabled is False
    assert (batch.target_date, batch.window_start, batch.window_end) == (
        date(2026, 10, 16), date(2026, 10, 9), date(2026, 10, 15)
    )
    assert {task.store_id for task in batch.stores} == {active_a.id, active_b.id}

    monkeypatch.setattr(store_pool_reset, "_reset_target_date", lambda: date(2026, 10, 17))
    assert (batch.target_date, batch.window_start, batch.window_end) == (
        date(2026, 10, 16), date(2026, 10, 9), date(2026, 10, 15)
    )
    assert db_session.get(Product, inactive_product.id).status == "active"


def test_pool_reset_start_requires_selected_online_capable_worker_and_no_daily_claim(
    db_session, monkeypatch
):
    from app.services import browser_collection
    from app.services.store_pool_reset import start_pool_reset_batch
    from app.services.browser_collection import utcnow

    monkeypatch.setattr(
        browser_collection, "_target_date", lambda value=None: date(2026, 10, 16)
    )
    batch = _held_reset_batch(db_session, monkeypatch)
    with pytest.raises(ValueError, match="选择采集电脑"):
        start_pool_reset_batch(db_session, batch.id)

    worker = BrowserCollectionWorker(
        worker_id="reset-worker",
        label="Reset Worker",
        extension_version="0.6.90",
        capabilities=[],
        last_heartbeat_at=utcnow(),
    )
    control = db_session.get(BrowserCollectionControl, 1)
    control.selected_worker_id = worker.worker_id
    control.selected_worker_label = worker.label
    db_session.add(worker)
    db_session.flush()
    with pytest.raises(ValueError, match="不支持监控池重建"):
        start_pool_reset_batch(db_session, batch.id)

    worker.capabilities = ["store_pool_reset_v1"]
    run = BrowserCollectionRun(
        target_date=date(2026, 10, 16), trigger="MANUAL", status="RUNNING",
        phase="STORE_DISCOVERY", dispatch_enabled=True,
    )
    daily_store = db_session.query(Store).filter_by(aliexpress_store_id="1100000030").one()
    daily_item = BrowserCollectionItem(
        run=run,
        target_type="STORE",
        target_key=f"store:{daily_store.id}",
        store_id=daily_store.id,
        target_url=daily_store.url,
        position=1,
        status="RUNNING",
        claimed_at=utcnow(),
        lease_expires_at=utcnow() + timedelta(minutes=4),
        claimed_worker_id=worker.worker_id,
        claim_token="daily-claim-token",
    )
    db_session.add(daily_item)
    db_session.flush()
    with pytest.raises(ValueError, match="每日采集任务仍在运行"):
        start_pool_reset_batch(db_session, batch.id)
    assert batch.status == "PENDING"
    assert batch.dispatch_enabled is False
    assert daily_item.status == "RUNNING"
    assert run.dispatch_enabled is True

    daily_item.status = "PENDING"
    daily_item.claimed_at = None
    daily_item.lease_expires_at = None
    daily_item.claimed_worker_id = None
    daily_item.claim_token = None
    db_session.flush()
    started = start_pool_reset_batch(db_session, batch.id)
    assert started.status == "RUNNING"
    assert started.dispatch_enabled is True
    assert run.dispatch_enabled is True


def test_pool_reset_api_creates_held_batch_only(client, db_session, monkeypatch):
    from app.services import store_pool_reset

    _make_store(db_session, "1100000033")
    monkeypatch.setattr(store_pool_reset, "_reset_target_date", lambda: date(2026, 10, 16))

    rejected = client.post("/api/browser-collection/pool-resets", json={"confirm_replace": False})
    assert rejected.status_code == 422
    created = client.post("/api/browser-collection/pool-resets", json={"confirm_replace": True})
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "PENDING"
    assert created.json()["dispatch_enabled"] is False
    assert len(created.json()["stores"]) == 1
    latest = client.get("/api/browser-collection/pool-resets/latest")
    assert latest.status_code == 200
    assert latest.json()["id"] == created.json()["id"]


def test_daily_claim_is_withheld_during_pool_reset_without_daily_run_mutation(
    client, db_session, monkeypatch
):
    from app.services import browser_collection
    from app.services import store_pool_reset
    from app.services.browser_collection import utcnow

    monkeypatch.setattr(
        browser_collection, "_target_date", lambda value=None: date(2026, 10, 16)
    )
    store = _make_store(db_session, "1100000034")
    batch = _held_reset_batch(db_session, monkeypatch, store_ids=())
    task = batch.stores[0]
    batch.status = "RUNNING"
    batch.dispatch_enabled = True
    worker = BrowserCollectionWorker(
        worker_id="reset-worker-active",
        label="Reset Worker",
        extension_version="0.6.90",
        capabilities=["store_pool_reset_v1"],
        last_heartbeat_at=utcnow(),
    )
    control = db_session.get(BrowserCollectionControl, 1)
    control.selected_worker_id = worker.worker_id
    control.selected_worker_label = worker.label
    db_session.add(worker)
    run = BrowserCollectionRun(
        target_date=date(2026, 10, 16), trigger="MANUAL", status="RUNNING",
        phase="STORE_DISCOVERY", dispatch_enabled=True,
    )
    daily_item = BrowserCollectionItem(
        run=run, target_type="STORE", target_key=f"store:{store.id}", store_id=store.id,
        target_url=store.url, position=1, status="PENDING",
    )
    db_session.add(daily_item)
    db_session.commit()
    old_last_heartbeat = run.last_heartbeat_at

    response = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": worker.worker_id}
    )

    assert response.status_code == 200, response.text
    assert response.json()["item"] is None
    assert response.json()["run"]["status"] == "RUNNING"
    db_session.refresh(run)
    db_session.refresh(daily_item)
    assert run.status == "RUNNING"
    assert run.dispatch_enabled is True
    assert run.last_heartbeat_at == old_last_heartbeat
    assert daily_item.status == "PENDING"
    assert task.status == "PENDING"


def _start_reset_for_page_tests(db_session, monkeypatch):
    from app.services import store_pool_reset
    from app.services.browser_collection import utcnow

    _make_store(db_session, "1100000090")
    batch = _held_reset_batch(db_session, monkeypatch, store_ids=())
    worker = BrowserCollectionWorker(
        worker_id="reset-page-worker",
        label="Reset Page Worker",
        extension_version="0.6.90",
        capabilities=["store_pool_reset_v1"],
        last_heartbeat_at=utcnow(),
    )
    control = db_session.get(BrowserCollectionControl, 1)
    control.selected_worker_id = worker.worker_id
    control.selected_worker_label = worker.label
    db_session.add(worker)
    db_session.flush()
    store_pool_reset.start_pool_reset_batch(db_session, batch.id)
    return batch, worker


def _reset_page(store_id, page_number, candidates, *, has_next, complete=True, known=True):
    page_url = (
        f"https://www.aliexpress.com/store/{store_id}/pages/all-items.html"
        f"?sortType=bestmatch_sort&shop_sortType=orders_desc&page={page_number}"
    )
    next_page_url = (
        f"https://www.aliexpress.com/store/{store_id}/pages/all-items.html"
        f"?sortType=bestmatch_sort&shop_sortType=orders_desc&page={page_number + 1}"
        if has_next else None
    )
    return {
        "page_url": page_url,
        "pagination_known": known,
        "has_next_page": has_next,
        "next_page_url": next_page_url,
        "scan_complete": complete,
        "candidates": candidates,
        "diagnostics": {},
    }


def _reset_page_candidate(index, target_date=date(2026, 10, 16)):
    product_id = f"100500{index:07d}"
    return {
        "platform_product_id": product_id,
        "url": f"https://www.aliexpress.com/item/{product_id}.html",
        "title": f"Candidate {index}",
        "historical_sales": [
            {
                "date": (target_date - timedelta(days=offset)).isoformat(),
                "value": index + offset,
                "value_type": "daily_increment",
            }
            for offset in range(1, 8)
        ],
        "history_source": "ixspy_visible_daily_increment",
        "history_daily_capture_complete": True,
        "history_expected_dates": [
            (target_date - timedelta(days=offset)).isoformat()
            for offset in range(1, 8)
        ],
        "history_missing_dates": [],
    }


def test_pool_reset_claims_one_orders_page_and_finalizes_only_after_sixty_candidates(
    db_session, monkeypatch
):
    from app.services.store_pool_reset import (
        claim_pool_reset_store,
        record_pool_reset_page,
    )

    batch, worker = _start_reset_for_page_tests(db_session, monkeypatch)
    first = claim_pool_reset_store(db_session, worker.worker_id)
    assert first is not None
    _, task, first_token, limit = first
    assert limit == 50
    first_url = task.next_page_url
    page_one = _reset_page(
        task.platform_store_id,
        1,
        [_reset_page_candidate(index) for index in range(1, 51)],
        has_next=True,
    )
    page_one["page_url"] = first_url

    checkpoint = record_pool_reset_page(
        db_session,
        task.id,
        worker.worker_id,
        first_token,
        task.platform_store_id,
        page_one,
    )
    db_session.refresh(task)
    assert task.status == "PENDING"
    assert task.scanned_candidate_count == 50
    assert task.scanned_page_count == 1
    assert len(task.staged_candidates) == 50
    assert "page=2" in task.next_page_url
    assert db_session.query(Product).filter_by(store_id=task.store_id).count() == 0
    assert checkpoint.status == "RUNNING"

    second = claim_pool_reset_store(db_session, worker.worker_id)
    assert second is not None
    _, task, second_token, limit = second
    assert limit == 10
    page_two = _reset_page(
        task.platform_store_id,
        2,
        [_reset_page_candidate(index) for index in range(51, 61)],
        has_next=True,
    )
    page_two["page_url"] = task.next_page_url

    completed = record_pool_reset_page(
        db_session,
        task.id,
        worker.worker_id,
        second_token,
        task.platform_store_id,
        page_two,
    )
    db_session.refresh(task)
    assert task.status == "SUCCEEDED"
    assert task.scanned_candidate_count == 60
    assert task.scanned_page_count == 2
    assert task.next_page_url is None
    assert db_session.query(Product).filter_by(store_id=task.store_id, status="active").count() == 20
    assert completed.status == "COMPLETED"
    assert len(batch.stores) == 1


def test_incomplete_pool_reset_page_does_not_advance_or_replace_pool(db_session, monkeypatch):
    from app.services.store_pool_reset import claim_pool_reset_store, record_pool_reset_page

    _, worker = _start_reset_for_page_tests(db_session, monkeypatch)
    claimed = claim_pool_reset_store(db_session, worker.worker_id)
    _, task, token, _ = claimed
    original_url = task.next_page_url
    incomplete = _reset_page(
        task.platform_store_id,
        1,
        [_reset_page_candidate(1)],
        has_next=True,
        complete=False,
    )
    incomplete["page_url"] = original_url

    with pytest.raises(ValueError, match="完整"):
        record_pool_reset_page(
            db_session,
            task.id,
            worker.worker_id,
            token,
            task.platform_store_id,
            incomplete,
        )

    db_session.refresh(task)
    assert task.status == "RUNNING"
    assert task.scanned_candidate_count == 0
    assert task.next_page_url == original_url
    assert task.staged_candidates == []
    assert db_session.query(Product).filter_by(store_id=task.store_id).count() == 0


def test_pool_reset_page_result_is_idempotent_for_same_claim_and_rejects_changed_replay(
    db_session, monkeypatch
):
    from app.services.store_pool_reset import claim_pool_reset_store, record_pool_reset_page

    _, worker = _start_reset_for_page_tests(db_session, monkeypatch)
    _, task, token, _ = claim_pool_reset_store(db_session, worker.worker_id)
    page = _reset_page(
        task.platform_store_id,
        1,
        [_reset_page_candidate(1)],
        has_next=False,
    )
    page["page_url"] = task.next_page_url

    first = record_pool_reset_page(
        db_session, task.id, worker.worker_id, token, task.platform_store_id, page
    )
    task_id = task.id
    second = record_pool_reset_page(
        db_session, task_id, worker.worker_id, token, task.platform_store_id, page
    )
    assert first.id == second.id
    assert db_session.query(Product).filter_by(store_id=task.store_id, status="active").count() == 1

    changed = {**page, "diagnostics": {"changed": True}}
    with pytest.raises(ValueError, match="重复提交"):
        record_pool_reset_page(
            db_session, task_id, worker.worker_id, token, task.platform_store_id, changed
        )


def test_pool_reset_challenge_keeps_claim_and_resume_requires_same_orders_page(
    db_session, monkeypatch
):
    from app.services.store_pool_reset import (
        claim_pool_reset_store,
        record_pool_reset_challenge,
        resume_pool_reset_after_challenge,
    )

    batch, worker = _start_reset_for_page_tests(db_session, monkeypatch)
    _, task, token, _ = claim_pool_reset_store(db_session, worker.worker_id)
    page_url = task.claimed_page_url

    paused = record_pool_reset_challenge(
        db_session,
        task.id,
        worker.worker_id,
        token,
        "平台需要验证",
        page_url,
    )
    db_session.refresh(task)
    assert paused.status == "NEEDS_VERIFICATION"
    assert task.status == "NEEDS_VERIFICATION"
    assert task.claim_token == token
    assert task.next_page_url == page_url

    with pytest.raises(ValueError, match="已改变"):
        resume_pool_reset_after_challenge(
            db_session,
            task.id,
            worker.worker_id,
            token,
            f"https://www.aliexpress.com/store/1100000091/pages/all-items.html?shop_sortType=orders_desc",
        )

    resumed = resume_pool_reset_after_challenge(
        db_session, task.id, worker.worker_id, token, page_url
    )
    db_session.refresh(task)
    assert resumed.status == "RUNNING"
    assert task.status == "RUNNING"
    assert task.claim_token == token
    assert batch.id == resumed.id


def test_pool_reset_failed_store_retry_resumes_same_page_and_keeps_old_pool(
    db_session, monkeypatch
):
    from app.services.store_pool_reset import (
        claim_pool_reset_store,
        record_pool_reset_failure,
        retry_pool_reset_store,
    )

    _, worker = _start_reset_for_page_tests(db_session, monkeypatch)
    store = db_session.query(Store).filter_by(aliexpress_store_id="1100000090").one()
    existing = Product(
        store_id=store.id,
        aliexpress_product_id="1005009999000",
        url="https://www.aliexpress.com/item/1005009999000.html",
        status="active",
    )
    db_session.add(existing)
    db_session.flush()
    claimed = claim_pool_reset_store(db_session, worker.worker_id)
    _, task, token, _ = claimed
    first_url = task.claimed_page_url

    record_pool_reset_failure(
        db_session, task.id, worker.worker_id, token, "page_timeout", "page timeout"
    )
    claimed_again = claim_pool_reset_store(db_session, worker.worker_id)
    _, task, token, _ = claimed_again
    assert task.claimed_page_url == first_url
    record_pool_reset_failure(
        db_session, task.id, worker.worker_id, token, "page_timeout", "page timeout"
    )
    db_session.refresh(task)
    assert task.status == "FAILED"
    assert db_session.get(Product, existing.id).status == "active"

    retried = retry_pool_reset_store(db_session, task.id)
    db_session.refresh(task)
    assert retried.status == "PENDING"
    assert retried.dispatch_enabled is False
    assert task.status == "PENDING"
    assert task.page_attempt_count == 0
    assert task.next_page_url == first_url
    assert db_session.get(Product, existing.id).status == "active"


def test_pool_reset_api_claim_heartbeat_and_page_do_not_mutate_daily_run(
    client, db_session, monkeypatch
):
    from app.services import store_pool_reset
    from app.services.browser_collection import utcnow

    store = _make_store(db_session, "1100000092")
    monkeypatch.setattr(store_pool_reset, "_reset_target_date", lambda: date(2026, 10, 16))
    batch = store_pool_reset.create_pool_reset_batch(db_session, confirm_replace=True)
    worker = BrowserCollectionWorker(
        worker_id="reset-api-worker",
        label="Reset API Worker",
        extension_version="0.6.90",
        capabilities=["store_pool_reset_v1"],
        last_heartbeat_at=utcnow(),
    )
    control = db_session.get(BrowserCollectionControl, 1)
    control.selected_worker_id = worker.worker_id
    control.selected_worker_label = worker.label
    db_session.add(worker)
    db_session.commit()
    store_pool_reset.start_pool_reset_batch(db_session, batch.id)

    claim_response = client.post(
        "/api/browser-collection/pool-resets/claim",
        json={"worker_id": worker.worker_id},
    )
    assert claim_response.status_code == 200, claim_response.text
    claim = claim_response.json()
    assert claim["store"]["platform_store_id"] == store.aliexpress_store_id
    assert claim["candidate_limit"] == 50
    assert claim["target_url"] == claim["store"]["next_page_url"]

    heartbeat = client.post(
        "/api/browser-collection/pool-resets/heartbeat",
        json={
            "worker_id": worker.worker_id,
            "claim_token": claim["claim_token"],
            "capabilities": ["store_pool_reset_v1"],
            "extension_version": "0.6.90",
        },
    )
    assert heartbeat.status_code == 200, heartbeat.text
    page_url = claim["target_url"]
    page = {
        "page_url": page_url,
        "pagination_known": True,
        "has_next_page": False,
        "next_page_url": None,
        "scan_complete": True,
        "candidates": [_reset_page_candidate(92)],
        "diagnostics": {"scanned_candidate_count": 1},
    }
    accepted = client.post(
        f"/api/browser-collection/pool-resets/tasks/{claim['store']['id']}/page",
        json={
            "worker_id": worker.worker_id,
            "claim_token": claim["claim_token"],
            "expected_store_id": store.aliexpress_store_id,
            "page": page,
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "COMPLETED"
    assert db_session.query(Product).filter_by(store_id=store.id, status="active").count() == 1
    assert db_session.query(BrowserCollectionRun).count() == 0

