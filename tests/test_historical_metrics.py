from datetime import date

from app.db.models import ProductDailyMetric, ProductSnapshot


def create_product(client, product_id="100500760"):
    store = client.post(
        "/api/stores",
        json={"name": "History Store", "url": "https://www.aliexpress.com/store/776000"},
    ).json()
    return client.post(
        "/api/products",
        json={"store_id": store["id"], "aliexpress_product_id": product_id},
    ).json()


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


def test_cumulative_history_requires_consecutive_non_decreasing_points(client, db_session):
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
    imported = [
        metric
        for metric in metrics
        if metric.calculation_method == "ixspy_visible_cumulative_delta"
    ]
    assert len(imported) == 1
    assert imported[0].metric_date == date(2026, 9, 20)
    assert imported[0].estimated_sales == 11
