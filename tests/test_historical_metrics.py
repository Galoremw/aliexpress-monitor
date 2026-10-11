from datetime import date, datetime

from app.db.models import ProductDailyMetric, ProductSnapshot, StoreDailyMetric
from app.services.historical_metrics import (
    ixspy_daily_metric_coverage,
    repair_recent_ixspy_daily_metrics,
    rolling_history_coverage,
)


def create_product(client, product_id="100500760"):
    store = client.post(
        "/api/stores",
        json={"name": "History Store", "url": "https://www.aliexpress.com/store/776000"},
    ).json()
    return client.post(
        "/api/products",
        json={"store_id": store["id"], "aliexpress_product_id": product_id},
    ).json()


def test_rolling_coverage_counts_ixspy_daily_values_independently_of_page_sold_total():
    dates = [date(2026, 9, day) for day in range(17, 24)]
    points = [
        {"date": metric_date.isoformat(), "value": 13, "value_type": "daily_increment"}
        for metric_date in dates
    ]

    assert rolling_history_coverage(points, target_date=date(2026, 9, 24), sold_count=12) == 7


def test_rolling_coverage_counts_typed_daily_increment_even_if_cumulative_value_matches():
    points = [
        {"date": "2026-09-24", "value": 4_147, "value_type": "daily_increment"},
        {"date": "2026-09-25", "value": 4_147, "value_type": "cumulative_total"},
    ]

    assert rolling_history_coverage(points, target_date=date(2026, 9, 26)) == 1


def test_rolling_coverage_ignores_cumulative_points():
    target = date(2026, 9, 24)
    seven_points = [
        {"date": date(2026, 9, day).isoformat(), "value": day * 10, "value_type": "cumulative_total"}
        for day in range(17, 24)
    ]
    eight_points = [
        {"date": date(2026, 9, day).isoformat(), "value": day * 10, "value_type": "cumulative_total"}
        for day in range(16, 24)
    ]

    assert rolling_history_coverage(seven_points, target, sold_count=300) == 0
    assert rolling_history_coverage(eight_points, target, sold_count=300) == 0


def test_metrics_api_hides_legacy_estimates_but_keeps_them_in_storage(client, db_session):
    product = create_product(client, "100500760888")
    store_id = product["store_id"]
    db_session.add_all(
        [
            ProductDailyMetric(
                product_id=product["id"],
                metric_date=date(2026, 9, 22),
                estimated_sales=19,
                is_estimable=True,
                reason="estimated_from_public_cumulative_delta",
                calculation_method="public_cumulative_sold_delta",
            ),
            ProductDailyMetric(
                product_id=product["id"],
                metric_date=date(2026, 9, 23),
                estimated_sales=0,
                is_estimable=True,
                reason="ixspy_visible_daily_increment",
                calculation_method="ixspy_visible_daily_increment",
            ),
            StoreDailyMetric(
                store_id=store_id,
                metric_date=date(2026, 9, 22),
                estimated_sales=19,
                calculation_method="public_cumulative_sold_delta",
            ),
            StoreDailyMetric(
                store_id=store_id,
                metric_date=date(2026, 9, 23),
                estimated_sales=0,
                calculation_method="sum_of_ixspy_visible_daily_increments",
            ),
        ]
    )
    db_session.commit()

    product_metrics = client.get(f"/api/products/{product['id']}/daily-metrics")
    store_metrics = client.get(f"/api/stores/{store_id}/daily-metrics")

    assert product_metrics.status_code == 200
    assert [metric["estimated_sales"] for metric in product_metrics.json()] == [0]
    assert product_metrics.json()[0]["calculation_method"] == "ixspy_visible_daily_increment"
    assert store_metrics.status_code == 200
    assert store_metrics.json() == []
    assert db_session.query(ProductDailyMetric).filter_by(product_id=product["id"]).count() == 2
    assert db_session.query(StoreDailyMetric).filter_by(store_id=store_id).count() == 2


def test_visible_history_points_are_saved_and_become_daily_metrics(client, db_session):
    product = create_product(client)
    response = client.post(
        "/api/collection/browser-extension",
        json={
            "platform_product_id": "100500760",
            "url": "https://www.aliexpress.com/item/100500760.html",
            "title": "History Product",
            "sold_count": 300,
            "captured_at": "2026-09-22T10:00:00+08:00",
            "historical_sales": [
                {"date": "2026-09-19", "value": 7, "value_type": "daily_increment"},
                {"date": "2026-09-20", "value": 11, "value_type": "daily_increment"},
                {"date": "2026-09-21", "value": 4, "value_type": "daily_increment"},
            ],
            "raw_data": {"history_source": "visible_dom_table_only"},
        },
    )

    assert response.status_code == 201
    snapshot = db_session.query(ProductSnapshot).filter_by(product_id=product["id"]).one()
    assert len(snapshot.raw_data["historical_sales"]) == 3
    metrics = {
        metric.metric_date: metric
        for metric in db_session.query(ProductDailyMetric)
        .filter_by(product_id=product["id"])
        .all()
    }
    assert metrics[date(2026, 9, 19)].estimated_sales == 7
    assert metrics[date(2026, 9, 20)].estimated_sales == 11
    assert metrics[date(2026, 9, 21)].reason == "ixspy_visible_daily_increment"


def test_explicit_zero_values_are_saved_but_unobserved_days_remain_missing(client, db_session):
    product = create_product(client, "100500780")
    response = client.post(
        "/api/collection/browser-extension",
        json={
            "platform_product_id": "100500780",
            "url": "https://www.aliexpress.com/item/100500780.html",
            "title": "Seven Day Visible Trend",
            "sold_count": 200,
            "captured_at": "2026-09-26T10:00:00+08:00",
            "historical_sales": [
                {"date": "2026-09-19", "value": 1, "value_type": "daily_increment"},
                {"date": "2026-09-21", "value": 2, "value_type": "daily_increment"},
                {"date": "2026-09-23", "value": 0, "value_type": "daily_increment"},
                {"date": "2026-09-25", "value": 4, "value_type": "daily_increment"},
            ],
            "raw_data": {"history_daily_capture_complete": True},
        },
    )

    assert response.status_code == 201
    metrics = {
        metric.metric_date: metric
        for metric in db_session.query(ProductDailyMetric).filter_by(product_id=product["id"])
        if date(2026, 9, 19) <= metric.metric_date <= date(2026, 9, 25)
    }
    assert set(metrics) == {
        date(2026, 9, 19), date(2026, 9, 21), date(2026, 9, 23), date(2026, 9, 25)
    }
    assert [metrics[date(2026, 9, day)].estimated_sales for day in (19, 21, 23, 25)] == [1, 2, 0, 4]
    assert metrics[date(2026, 9, 23)].calculation_method == "ixspy_visible_daily_increment"
    assert date(2026, 9, 20) not in metrics


def test_incomplete_daily_capture_does_not_fabricate_zeroes(client, db_session):
    product = create_product(client, "100500781")
    response = client.post(
        "/api/collection/browser-extension",
        json={
            "platform_product_id": "100500781",
            "url": "https://www.aliexpress.com/item/100500781.html",
            "title": "Incomplete Visible Trend",
            "sold_count": 200,
            "captured_at": "2026-09-26T10:00:00+08:00",
            "historical_sales": [
                {"date": "2026-09-19", "value": 1, "value_type": "daily_increment"},
                {"date": "2026-09-21", "value": 2, "value_type": "daily_increment"},
                {"date": "2026-09-25", "value": 4, "value_type": "daily_increment"},
            ],
        },
    )

    assert response.status_code == 201
    metrics = [
        metric
        for metric in db_session.query(ProductDailyMetric).filter_by(product_id=product["id"])
        if date(2026, 9, 19) <= metric.metric_date <= date(2026, 9, 25)
    ]
    assert len(metrics) == 3
    assert all(metric.calculation_method == "ixspy_visible_daily_increment" for metric in metrics)


def test_ixspy_daily_series_is_not_rejected_by_unrelated_page_sold_total(client, db_session):
    product = create_product(client, "100500782")
    response = client.post(
        "/api/collection/browser-extension",
        json={
            "platform_product_id": "100500782",
            "url": "https://www.aliexpress.com/item/100500782.html",
            "title": "Mislabeled Total Series",
            "sold_count": 12,
            "captured_at": "2026-09-26T10:00:00+08:00",
            "historical_sales": [
                {"date": "2026-09-19", "value": 4, "value_type": "daily_increment"},
                {"date": "2026-09-22", "value": 4, "value_type": "daily_increment"},
                {"date": "2026-09-25", "value": 5, "value_type": "daily_increment"},
            ],
            "raw_data": {"history_daily_capture_complete": True},
        },
    )

    assert response.status_code == 201
    metrics = db_session.query(ProductDailyMetric).filter_by(
        product_id=product["id"], calculation_method="ixspy_visible_daily_increment"
    ).all()
    assert {metric.metric_date: metric.estimated_sales for metric in metrics} == {
        date(2026, 9, 19): 4,
        date(2026, 9, 22): 4,
        date(2026, 9, 25): 5,
    }


def test_ixspy_daily_value_takes_precedence_over_cumulative_history(client, db_session):
    product = create_product(client, "100500783")
    baseline = client.post(
        "/api/collection/browser-extension",
        json={
            "platform_product_id": "100500783",
            "url": "https://www.aliexpress.com/item/100500783.html",
            "title": "Mislabeled IXSPY Tooltip Product",
            "sold_count": 5_135,
            "captured_at": "2026-09-25T10:00:00+08:00",
            "historical_sales": [
                {"date": "2026-09-23", "value": 5_118, "value_type": "cumulative_total"},
                {"date": "2026-09-24", "value": 5_118, "value_type": "cumulative_total"},
            ],
        },
    )
    assert baseline.status_code == 201

    response = client.post(
        "/api/collection/browser-extension",
        json={
            "platform_product_id": "100500783",
            "url": "https://www.aliexpress.com/item/100500783.html",
            "title": "Mislabeled IXSPY Tooltip Product",
            "sold_count": 5_155,
            "captured_at": "2026-09-26T10:00:00+08:00",
            "historical_sales": [
                {"date": "2026-09-24", "value": 5_135, "value_type": "daily_increment"},
                {"date": "2026-09-25", "value": 5_135, "value_type": "cumulative_total"},
            ],
        },
    )

    assert response.status_code == 201
    snapshot = db_session.query(ProductSnapshot).filter_by(product_id=product["id"]).order_by(
        ProductSnapshot.collected_at.desc()
    ).first()
    assert snapshot.sold_count is None
    assert snapshot.cumulative_sold is None
    assert snapshot.raw_data["historical_sales"][0]["value"] == 5_135
    assert len(snapshot.raw_data["historical_sales"]) == 1
    metric = db_session.query(ProductDailyMetric).filter_by(
        product_id=product["id"], metric_date=date(2026, 9, 24)
    ).one()
    assert metric.estimated_sales == 5_135
    assert metric.is_estimable is True
    assert metric.calculation_method == "ixspy_visible_daily_increment"


def test_repair_uses_complete_current_ixspy_snapshot_without_changing_raw(client, db_session):
    product = create_product(client, "100500784")
    store_id = product["store_id"]
    values = (51, 30, 21, 29, 23, 19, 26)
    raw_data = {
        "history_source": "ixspy_visible_daily_increment",
        "history_daily_capture_complete": True,
        "historical_sales": [
            {"date": f"2026-09-{day}", "value": value, "value_type": "daily_increment"}
            for day, value in zip(("22", "23", "24", "25", "26", "27", "28"), values, strict=True)
        ],
    }
    snapshot = ProductSnapshot(
        product_id=product["id"],
        collected_at=datetime(2026, 9, 29, 2),
        captured_at=datetime(2026, 9, 29, 2),
        collector_name="chrome_extension",
        collector_version="extension-content-0.6.38",
        parse_status="success",
        source="CHROME_EXTENSION",
        status="VALID",
        sold_count=5_155,
        raw_data=raw_data,
    )
    db_session.add(snapshot)
    db_session.flush()
    metric = ProductDailyMetric(
        product_id=product["id"],
        metric_date=date(2026, 9, 24),
        estimated_sales=0,
        is_estimable=True,
        reason="ixspy_visible_cumulative_delta",
        end_snapshot_id=None,
        calculation_method="ixspy_visible_cumulative_delta",
    )
    db_session.add(metric)
    db_session.commit()

    assert repair_recent_ixspy_daily_metrics(
        db_session, product_ids=[product["id"]], target_date=date(2026, 9, 29)
    ) == {"products": 1, "metrics": 7}

    db_session.refresh(metric)
    db_session.refresh(snapshot)
    store_metric = db_session.query(StoreDailyMetric).filter_by(
        store_id=store_id, metric_date=date(2026, 9, 24)
    ).one()
    assert metric.estimated_sales == 21
    assert metric.is_estimable is True
    assert metric.calculation_method == "ixspy_visible_daily_increment"
    assert store_metric.estimated_sales == 21
    assert {
        row.metric_date: row.estimated_sales
        for row in db_session.query(StoreDailyMetric).filter_by(store_id=store_id)
    } == {
        date(2026, 9, day): value
        for day, value in zip(range(22, 29), values, strict=True)
    }
    assert snapshot.raw_data == raw_data


def test_repair_ignores_cumulative_like_series_mislabeled_as_daily(client, db_session):
    product = create_product(client, "100500785")
    values = [26170, 26213, 26257, 26245, 26202, 26262, 26263]
    dates = [date(2026, 10, day) for day in range(1, 8)]
    db_session.add(ProductSnapshot(
        product_id=product["id"],
        collected_at=datetime(2026, 10, 8, 2),
        captured_at=datetime(2026, 10, 8, 2),
        collector_name="chrome_extension",
        collector_version="extension-content-0.6.66",
        parse_status="success",
        source="CHROME_EXTENSION",
        status="VALID",
        raw_data={
            "history_source": "ixspy_visible_daily_increment",
            "history_daily_capture_complete": True,
            "historical_sales": [
                {"date": day.isoformat(), "value": value, "value_type": "daily_increment"}
                for day, value in zip(dates, values, strict=True)
            ],
        },
    ))
    db_session.commit()

    assert repair_recent_ixspy_daily_metrics(
        db_session, product_ids=[product["id"]], target_date=date(2026, 10, 8)
    ) == {"products": 0, "metrics": 0}
    assert db_session.query(ProductDailyMetric).filter_by(product_id=product["id"]).count() == 0


def test_same_date_daily_increment_wins_over_cumulative_total(client, db_session):
    product = create_product(client, "100500761")
    response = client.post(
        "/api/collection/browser-extension",
        json={
            "platform_product_id": "100500761",
            "url": "https://www.aliexpress.com/item/100500761.html",
            "title": "Current Day Increment Product",
            "sold_count": 4_937,
            "captured_at": "2026-09-25T10:00:00+08:00",
            "historical_sales": [
                {"date": "2026-09-24", "value": 4_923, "value_type": "cumulative_total"},
                {"date": "2026-09-24", "value": 14, "value_type": "daily_increment"},
            ],
        },
    )

    assert response.status_code == 201
    snapshot = db_session.query(ProductSnapshot).filter_by(product_id=product["id"]).one()
    assert len(snapshot.raw_data["historical_sales"]) == 1
    assert snapshot.raw_data["historical_sales"][0]["value_type"] == "daily_increment"
    assert snapshot.sold_count is None
    metric = db_session.query(ProductDailyMetric).filter_by(
        product_id=product["id"], metric_date=date(2026, 9, 24)
    ).one()
    assert metric.estimated_sales == 14
    assert metric.calculation_method == "ixspy_visible_daily_increment"


def test_latest_visible_zero_daily_increment_is_saved_as_zero(client, db_session):
    product = create_product(client, "100500762")
    response = client.post(
        "/api/collection/browser-extension",
        json={
            "platform_product_id": "100500762",
            "url": "https://www.aliexpress.com/item/100500762.html",
            "title": "Zero Sales Product",
            "sold_count": 4_937,
            "captured_at": "2026-09-25T10:00:00+08:00",
            "historical_sales": [
                {"date": "2026-09-24", "value": 4_937, "value_type": "cumulative_total"},
                {"date": "2026-09-24", "value": 0, "value_type": "daily_increment"},
            ],
        },
    )

    assert response.status_code == 201
    metric = db_session.query(ProductDailyMetric).filter_by(
        product_id=product["id"], metric_date=date(2026, 9, 24)
    ).one()
    assert metric.estimated_sales == 0
    assert metric.is_estimable is True
    assert metric.calculation_method == "ixspy_visible_daily_increment"


def test_ixspy_daily_increment_is_not_bounded_by_aliexpress_page_sold_count(client, db_session):
    product = create_product(client, "100500765")
    response = client.post(
        "/api/collection/browser-extension",
        json={
            "platform_product_id": "100500765",
            "url": "https://www.aliexpress.com/item/100500765.html",
            "title": "Implausible History Product",
            "sold_count": 12,
            "captured_at": "2026-09-22T10:00:00+08:00",
            "historical_sales": [
                {"date": "2026-09-21", "value": 13, "value_type": "daily_increment"},
            ],
        },
    )

    assert response.status_code == 201
    snapshot = db_session.query(ProductSnapshot).filter_by(product_id=product["id"]).one()
    assert snapshot.raw_data["historical_sales"][0]["value"] == 13
    metrics = db_session.query(ProductDailyMetric).filter_by(product_id=product["id"]).all()
    metric = db_session.query(ProductDailyMetric).filter_by(
        product_id=product["id"], metric_date=date(2026, 9, 21)
    ).one()
    assert metric.reason == "ixspy_visible_daily_increment"
    assert metric.estimated_sales == 13


def test_typed_daily_increment_is_imported_even_when_it_matches_nearby_cumulative_total(client, db_session):
    product = create_product(client, "100500769")
    response = client.post(
        "/api/collection/browser-extension",
        json={
            "platform_product_id": "100500769",
            "url": "https://www.aliexpress.com/item/100500769.html",
            "title": "Mislabeled Near-Total Daily Point",
            "sold_count": 4_149,
            "captured_at": "2026-09-26T10:00:00+08:00",
            "historical_sales": [
                {"date": "2026-09-24", "value": 4_147, "value_type": "daily_increment"},
                {"date": "2026-09-25", "value": 4_147, "value_type": "cumulative_total"},
            ],
        },
    )

    assert response.status_code == 201
    snapshot = db_session.query(ProductSnapshot).filter_by(product_id=product["id"]).one()
    assert snapshot.raw_data["historical_sales"][0]["value"] == 4_147
    metric = db_session.query(ProductDailyMetric).filter_by(
        product_id=product["id"], metric_date=date(2026, 9, 24)
    ).one_or_none()
    assert metric is not None
    assert metric.is_estimable is True
    assert metric.estimated_sales == 4_147
    assert metric.calculation_method == "ixspy_visible_daily_increment"


def test_later_snapshot_does_not_invalidate_ixspy_value_against_page_sold_count(client, db_session):
    product = create_product(client, "100500766")
    first = client.post(
        "/api/collection/browser-extension",
        json={
            "platform_product_id": "100500766",
            "url": "https://www.aliexpress.com/item/100500766.html",
            "title": "Previously Misclassified Product",
            "sold_count": 12,
            "captured_at": "2026-09-21T10:00:00+08:00",
        },
    )
    assert first.status_code == 201
    snapshot = db_session.query(ProductSnapshot).filter_by(product_id=product["id"]).one()
    snapshot.raw_data = {
        "historical_sales": [
            {"date": "2026-09-20", "value": 13, "value_type": "daily_increment"},
        ]
    }
    metric = ProductDailyMetric(
        product_id=product["id"],
        metric_date=date(2026, 9, 20),
        estimated_sales=13,
        is_estimable=True,
        reason="ixspy_visible_daily_increment",
        start_snapshot_id=None,
        end_snapshot_id=snapshot.id,
        calculation_method="ixspy_visible_daily_increment",
    )
    db_session.add(metric)
    db_session.commit()

    refreshed = client.post(
        "/api/collection/browser-extension",
        json={
            "platform_product_id": "100500766",
            "url": "https://www.aliexpress.com/item/100500766.html",
            "title": "Previously Misclassified Product",
            "sold_count": 12,
            "captured_at": "2026-09-22T10:00:00+08:00",
        },
    )

    assert refreshed.status_code == 201
    db_session.refresh(metric)
    assert metric.is_estimable is True
    assert metric.estimated_sales == 13
    assert metric.reason == "ixspy_visible_daily_increment"


def test_recent_repair_ignores_legacy_snapshots_without_confirmed_daily_parser(client, db_session):
    product = create_product(client, "100500767")
    snapshot = ProductSnapshot(
        product_id=product["id"],
        collected_at=datetime(2026, 9, 22),
        captured_at=datetime(2026, 9, 22),
        collector_name="chrome_extension",
        collector_version="extension-content-0.6.7",
        parse_status="success",
        source="CHROME_EXTENSION",
        status="VALID",
        sold_count=12,
        raw_data={
            "historical_sales": [
                {"date": "2026-09-21", "value": 13, "value_type": "daily_increment"}
            ]
        },
    )
    db_session.add(snapshot)
    db_session.flush()
    metric = ProductDailyMetric(
        product_id=product["id"],
        metric_date=date(2026, 9, 21),
        estimated_sales=13,
        is_estimable=True,
        reason="ixspy_visible_daily_increment",
        end_snapshot_id=snapshot.id,
        calculation_method="ixspy_visible_daily_increment",
    )
    db_session.add(metric)
    db_session.commit()

    assert repair_recent_ixspy_daily_metrics(
        db_session, product_ids=[product["id"]], target_date=date(2026, 9, 29)
    ) == {"products": 0, "metrics": 0}

    db_session.refresh(metric)
    db_session.refresh(snapshot)
    assert metric.is_estimable is True
    assert metric.estimated_sales == 13
    assert snapshot.raw_data["historical_sales"][0]["value"] == 13


def test_recent_repair_does_not_reconcile_ixspy_increment_to_cumulative_total(client, db_session):
    product = create_product(client, "100500770")
    snapshot = ProductSnapshot(
        product_id=product["id"],
        collected_at=datetime(2026, 9, 26, 2),
        captured_at=datetime(2026, 9, 26, 2),
        collector_name="chrome_extension",
        collector_version="extension-content-0.6.26",
        parse_status="success",
        source="CHROME_EXTENSION",
        status="VALID",
        sold_count=4_149,
        raw_data={
            "historical_sales": [
                {"date": "2026-09-24", "value": 4_147, "value_type": "daily_increment"},
                {"date": "2026-09-25", "value": 4_147, "value_type": "cumulative_total"},
            ]
        },
    )
    db_session.add(snapshot)
    db_session.flush()
    metric = ProductDailyMetric(
        product_id=product["id"],
        metric_date=date(2026, 9, 24),
        estimated_sales=4_147,
        is_estimable=True,
        reason="ixspy_visible_daily_increment",
        end_snapshot_id=snapshot.id,
        calculation_method="ixspy_visible_daily_increment",
    )
    db_session.add(metric)
    db_session.commit()

    assert repair_recent_ixspy_daily_metrics(
        db_session, product_ids=[product["id"]], target_date=date(2026, 9, 29)
    ) == {"products": 0, "metrics": 0}

    db_session.refresh(metric)
    db_session.refresh(snapshot)
    assert metric.is_estimable is True
    assert metric.estimated_sales == 4_147
    assert metric.reason == "ixspy_visible_daily_increment"
    assert snapshot.raw_data["historical_sales"][0]["value"] == 4_147


def test_ixspy_coverage_excludes_public_page_derived_daily_estimates(client, db_session):
    product_id = create_product(client, "100500768")["id"]
    for day in range(18, 25):
        db_session.add(
            ProductDailyMetric(
                product_id=product_id,
                metric_date=date(2026, 9, day),
                estimated_sales=0,
                is_estimable=True,
                reason="estimated_from_public_cumulative_delta",
                calculation_method="public_cumulative_sold_delta",
            )
        )
    db_session.flush()

    assert ixspy_daily_metric_coverage(db_session, product_id, date(2026, 9, 25)) == 0

    for metric in db_session.query(ProductDailyMetric).filter_by(product_id=product_id):
        metric.calculation_method = "ixspy_visible_daily_increment"
    db_session.flush()

    assert ixspy_daily_metric_coverage(db_session, product_id, date(2026, 9, 25)) == 7


def test_new_visible_daily_increment_refreshes_existing_rolling_history(client, db_session):
    product = create_product(client, "100500763")
    initial = client.post(
        "/api/collection/browser-extension",
        json={
            "platform_product_id": "100500763",
            "url": "https://www.aliexpress.com/item/100500763.html",
            "title": "Rolling Trend Product",
            "sold_count": 300,
            "captured_at": "2026-09-22T10:00:00+08:00",
            "historical_sales": [
                {"date": f"2026-09-{day}", "value": value, "value_type": "daily_increment"}
                for day, value in (("16", 24), ("17", 16), ("18", 4), ("19", 1), ("20", 5), ("21", 9), ("22", 0))
            ],
        },
    )
    assert initial.status_code == 201

    refreshed = client.post(
        "/api/collection/browser-extension",
        json={
            "platform_product_id": "100500763",
            "url": "https://www.aliexpress.com/item/100500763.html",
            "title": "Rolling Trend Product",
            "sold_count": 304,
            "captured_at": "2026-09-24T10:00:00+08:00",
            "historical_sales": [
                {"date": "2026-09-23", "value": 4, "value_type": "daily_increment"},
            ],
        },
    )

    assert refreshed.status_code == 201
    metric = db_session.query(ProductDailyMetric).filter_by(
        product_id=product["id"], metric_date=date(2026, 9, 16)
    ).one()
    assert metric.estimated_sales == 24
    assert metric.calculation_method == "ixspy_visible_daily_increment"
    latest_metric = db_session.query(ProductDailyMetric).filter_by(
        product_id=product["id"], metric_date=date(2026, 9, 23)
    ).one()
    assert latest_metric.estimated_sales == 4


def test_daily_ixspy_refresh_updates_existing_points_in_rolling_seven_day_window(client, db_session):
    product = create_product(client, "100500764")
    dates = ("16", "17", "18", "19", "20", "21", "22")
    first_values = (24, 16, 4, 1, 5, 9, 0)
    refreshed_values = (26, 18, 5, 2, 6, 10, 3)

    for captured_at, values in (
        ("2026-09-22T10:00:00+08:00", first_values),
        ("2026-09-23T10:00:00+08:00", refreshed_values),
    ):
        response = client.post(
            "/api/collection/browser-extension",
            json={
                "platform_product_id": "100500764",
                "url": "https://www.aliexpress.com/item/100500764.html",
                "title": "Rolling Seven Day Product",
                "sold_count": 300,
                "captured_at": captured_at,
                "historical_sales": [
                    {"date": f"2026-09-{day}", "value": value, "value_type": "daily_increment"}
                    for day, value in zip(dates, values, strict=True)
                ],
            },
        )
        assert response.status_code == 201

    metrics = {
        metric.metric_date: metric.estimated_sales
        for metric in db_session.query(ProductDailyMetric).filter_by(product_id=product["id"])
    }
    assert [metrics[date.fromisoformat(f"2026-09-{day}")] for day in dates] == list(refreshed_values)
    zero_day = db_session.query(ProductDailyMetric).filter_by(
        product_id=product["id"], metric_date=date(2026, 9, 22)
    ).one()
    assert zero_day.estimated_sales == 3
    assert zero_day.is_estimable is True


def test_cumulative_history_does_not_create_daily_metrics(client, db_session):
    product = create_product(client, "100500761")
    response = client.post(
        "/api/collection/browser-extension",
        json={
            "platform_product_id": "100500761",
            "url": "https://www.aliexpress.com/item/100500761.html",
            "sold_count": 125,
            "captured_at": "2026-09-22T10:00:00+08:00",
            "historical_sales": [
                {"date": "2026-09-19", "value": 100, "value_type": "cumulative_total"},
                {"date": "2026-09-20", "value": 111, "value_type": "cumulative_total"},
                {"date": "2026-09-22", "value": 125, "value_type": "cumulative_total"},
            ],
        },
    )

    assert response.status_code == 201
    metrics = list(
        db_session.query(ProductDailyMetric).filter_by(product_id=product["id"])
    )
    assert metrics == []


def test_later_cumulative_point_does_not_use_previous_snapshot_as_baseline(client, db_session):
    product = create_product(client, "100500762")
    first = client.post(
        "/api/collection/browser-extension",
        json={
            "platform_product_id": "100500762",
            "url": "https://www.aliexpress.com/item/100500762.html",
            "sold_count": 111,
            "captured_at": "2026-09-22T10:00:00+08:00",
            "historical_sales": [
                {"date": "2026-09-20", "value": 100, "value_type": "cumulative_total"},
                {"date": "2026-09-21", "value": 111, "value_type": "cumulative_total"},
            ],
        },
    )
    assert first.status_code == 201

    second = client.post(
        "/api/collection/browser-extension",
        json={
            "platform_product_id": "100500762",
            "url": "https://www.aliexpress.com/item/100500762.html",
            "sold_count": 125,
            "captured_at": "2026-09-23T10:00:00+08:00",
            "historical_sales": [
                {"date": "2026-09-22", "value": 125, "value_type": "cumulative_total"},
            ],
        },
    )
    assert second.status_code == 201

    assert db_session.query(ProductDailyMetric).filter_by(product_id=product["id"]).count() == 0
