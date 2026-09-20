from datetime import date, datetime, timezone

from sqlalchemy import select

from app.db.models import Product, ProductSnapshot, Store
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


def test_estimates_daily_sales_from_public_cumulative_delta(db_session):
    _, product = seed_product(db_session)
    add_snapshot(db_session, product, datetime(2026, 9, 19, 4, tzinfo=timezone.utc), 100)
    add_snapshot(db_session, product, datetime(2026, 9, 20, 4, tzinfo=timezone.utc), 112)

    metrics = calculate_product_daily_metrics(db_session, product.id)

    assert metrics[-1].metric_date == date(2026, 9, 19)
    assert metrics[-1].estimated_sales == 12
    assert metrics[-1].reason == "estimated_from_public_cumulative_delta"


def test_uses_last_snapshot_within_each_day(db_session):
    _, product = seed_product(db_session)
    add_snapshot(db_session, product, datetime(2026, 9, 19, 2, tzinfo=timezone.utc), 10)
    add_snapshot(db_session, product, datetime(2026, 9, 20, 1, tzinfo=timezone.utc), 13)
    latest = add_snapshot(db_session, product, datetime(2026, 9, 20, 10, tzinfo=timezone.utc), 15)

    metric = calculate_product_daily_metrics(db_session, product.id)[-1]

    assert metric.estimated_sales == 5
    assert metric.end_snapshot_id == latest.id


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
        metric = calculate_product_daily_metrics(db_session, product.id)[-1]
        assert metric.estimated_sales is None
        assert metric.is_estimable is False
        assert metric.reason == reason


def test_store_aggregate_only_includes_active_monitored_products(db_session):
    store, active = seed_product(db_session, "201")
    _, unavailable = seed_product(db_session, "202")
    _, inactive = seed_product(db_session, "203", status="inactive")
    for product, sold in ((active, 15), (unavailable, None), (inactive, 99)):
        add_snapshot(db_session, product, datetime(2026, 9, 19, 2, tzinfo=timezone.utc), 10)
        add_snapshot(db_session, product, datetime(2026, 9, 20, 2, tzinfo=timezone.utc), sold)
        calculate_product_daily_metrics(db_session, product.id)

    aggregate = calculate_store_daily_metric(db_session, store.id, date(2026, 9, 19))

    assert aggregate.estimated_sales == 5
    assert aggregate.estimable_products == 1
    assert aggregate.unavailable_products == 1
    assert aggregate.product_scope_count == 2
