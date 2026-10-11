from datetime import date, datetime, timezone

from sqlalchemy import select

from app.db.models import Product, ProductDailyMetric, ProductSnapshot, Store
from app.services.metrics import calculate_product_daily_metrics, calculate_store_daily_metric


def seed_product(db_session, product_id="1005001", status="active"):
    store = db_session.scalar(select(Store).limit(1))
    if store is None:
        store = Store(name="Metric Store", url="https://www.aliexpress.com/store/330001")
        db_session.add(store)
        db_session.flush()
    product = Product(
        store_id=store.id,
        aliexpress_product_id=product_id,
        url=f"https://www.aliexpress.com/item/{product_id}.html",
        status=status,
    )
    db_session.add(product)
    db_session.flush()
    return store, product


def add_snapshot(db_session, product, when, sold, parse_status="success"):
    snapshot = ProductSnapshot(
        product_id=product.id,
        collected_at=when,
        collector_name="fixture",
        collector_version="1",
        parse_status=parse_status,
        cumulative_sold=sold,
        raw_payload={"sold": sold},
    )
    db_session.add(snapshot)
    db_session.flush()
    return snapshot


def test_public_cumulative_snapshots_do_not_create_daily_metrics(db_session):
    _, product = seed_product(db_session)
    add_snapshot(db_session, product, datetime(2026, 9, 19, 4, tzinfo=timezone.utc), 100)
    add_snapshot(db_session, product, datetime(2026, 9, 20, 4, tzinfo=timezone.utc), 112)

    metrics = calculate_product_daily_metrics(db_session, product.id)

    assert metrics == []


def test_multiple_public_total_snapshots_do_not_create_daily_metrics(db_session):
    _, product = seed_product(db_session)
    add_snapshot(db_session, product, datetime(2026, 9, 19, 2, tzinfo=timezone.utc), 10)
    add_snapshot(db_session, product, datetime(2026, 9, 20, 1, tzinfo=timezone.utc), 13)
    latest = add_snapshot(db_session, product, datetime(2026, 9, 20, 10, tzinfo=timezone.utc), 15)

    assert calculate_product_daily_metrics(db_session, product.id) == []
    assert latest.cumulative_sold == 15


def test_unchanged_cumulative_totals_do_not_become_zero_increment(db_session):
    _, product = seed_product(db_session, "1005010000000001")
    add_snapshot(db_session, product, datetime(2026, 9, 19, 4, tzinfo=timezone.utc), 4_923)
    add_snapshot(db_session, product, datetime(2026, 9, 20, 4, tzinfo=timezone.utc), 4_923)

    assert calculate_product_daily_metrics(db_session, product.id) == []


def test_inconsistent_snapshot_is_skipped_without_inventing_missing_daily_value(db_session):
    _, product = seed_product(db_session, "1005010000000002")
    previous = add_snapshot(db_session, product, datetime(2026, 9, 23, 14, 53, tzinfo=timezone.utc), 4_923)
    inconsistent = add_snapshot(db_session, product, datetime(2026, 9, 23, 17, 51, tzinfo=timezone.utc), 418)
    inconsistent.raw_data = {
        "visible_text_excerpt": "4,000+ sold",
        "historical_sales": [
            {"date": "2026-09-23", "value": 4_885, "value_type": "cumulative_total"}
        ],
    }
    current = add_snapshot(db_session, product, datetime(2026, 9, 24, 17, 12, tzinfo=timezone.utc), 4_937)
    stale_metric = ProductDailyMetric(
        product_id=product.id,
        metric_date=date(2026, 9, 24),
        estimated_sales=4_519,
        is_estimable=True,
        reason="estimated_from_public_cumulative_delta",
        start_snapshot_id=inconsistent.id,
        end_snapshot_id=current.id,
        calculation_method="public_cumulative_sold_delta",
    )
    db_session.add(stale_metric)
    db_session.flush()

    metrics = calculate_product_daily_metrics(db_session, product.id, timezone_name="Asia/Shanghai")

    assert stale_metric.estimated_sales == 4_519
    assert stale_metric.is_estimable is True
    assert stale_metric.reason == "estimated_from_public_cumulative_delta"
    assert metrics == []
    assert inconsistent.cumulative_sold == 418
    assert inconsistent.raw_data["visible_text_excerpt"] == "4,000+ sold"
    assert current.cumulative_sold == 4_937


def test_inconsistent_ixspy_cumulative_history_is_not_kept_as_daily_metric(db_session):
    _, product = seed_product(db_session, "1005010000000003")
    add_snapshot(db_session, product, datetime(2026, 9, 22, 12, tzinfo=timezone.utc), 4_200)
    inconsistent = add_snapshot(db_session, product, datetime(2026, 9, 23, 12, tzinfo=timezone.utc), 383)
    inconsistent.raw_data = {
        "visible_text_excerpt": "4,000+ sold",
        "historical_sales": [
            {"date": "2026-09-23", "value": 4_226, "value_type": "cumulative_total"}
        ],
    }
    stale_metric = ProductDailyMetric(
        product_id=product.id,
        metric_date=date(2026, 9, 23),
        estimated_sales=0,
        is_estimable=True,
        reason="ixspy_visible_cumulative_delta",
        end_snapshot_id=inconsistent.id,
        calculation_method="ixspy_visible_cumulative_delta",
    )
    db_session.add(stale_metric)
    db_session.flush()

    calculate_product_daily_metrics(db_session, product.id, timezone_name="Asia/Shanghai")

    assert stale_metric.estimated_sales == 0
    assert stale_metric.is_estimable is True
    assert stale_metric.reason == "ixspy_visible_cumulative_delta"
    assert inconsistent.raw_data["historical_sales"][0]["value"] == 4_226


def test_missing_decreased_failed_and_nonconsecutive_are_not_estimated(db_session):
    cases = [
        ("101", None, "success", 1, "cumulative_sold_missing"),
        ("102", 9, "success", 1, "cumulative_sold_decreased"),
        ("103", 12, "failed", 1, "snapshot_parse_failed"),
        ("104", 12, "success", 2, "non_consecutive_days"),
    ]
    for external_id, sold, status, day_gap, reason in cases:
        _, product = seed_product(db_session, external_id)
        add_snapshot(db_session, product, datetime(2026, 9, 18, 2, tzinfo=timezone.utc), 10)
        add_snapshot(
            db_session,
            product,
            datetime(2026, 9, 18 + day_gap, 2, tzinfo=timezone.utc),
            sold,
            status,
        )
        assert calculate_product_daily_metrics(db_session, product.id) == []


def test_store_aggregate_only_includes_active_monitored_products(db_session):
    store, active = seed_product(db_session, "201")
    _, unavailable = seed_product(db_session, "202")
    _, inactive = seed_product(db_session, "203", status="inactive")
    db_session.add_all(
        [
            ProductDailyMetric(
                product_id=active.id,
                metric_date=date(2026, 9, 19),
                estimated_sales=5,
                is_estimable=True,
                reason="ixspy_visible_daily_increment",
                calculation_method="ixspy_visible_daily_increment",
            ),
            ProductDailyMetric(
                product_id=inactive.id,
                metric_date=date(2026, 9, 19),
                estimated_sales=99,
                is_estimable=True,
                reason="ixspy_visible_daily_increment",
                calculation_method="ixspy_visible_daily_increment",
            ),
        ]
    )
    db_session.flush()

    aggregate = calculate_store_daily_metric(db_session, store.id, date(2026, 9, 19))

    assert aggregate.estimated_sales == 5
    assert aggregate.estimable_products == 1
    assert aggregate.unavailable_products == 1
    assert aggregate.product_scope_count == 2
