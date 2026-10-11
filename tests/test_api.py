from datetime import datetime, timezone

from app.db.models import Product, ProductSnapshot, Store
from app.services.metrics import calculate_product_daily_metrics, calculate_store_daily_metric


def seed_metrics(db_session):
    store = Store(name="API Store", url="https://www.aliexpress.com/store/550001")
    product = Product(
        store=store,
        aliexpress_product_id="100500555",
        url="https://www.aliexpress.com/item/100500555.html",
    )
    db_session.add_all(
        [
            ProductSnapshot(
                product=product,
                collected_at=datetime(2026, 9, 19, 2, tzinfo=timezone.utc),
                collector_name="fixture",
                collector_version="1",
                parse_status="success",
                cumulative_sold=20,
                raw_payload={"sold": 20},
            ),
            ProductSnapshot(
                product=product,
                collected_at=datetime(2026, 9, 20, 2, tzinfo=timezone.utc),
                collector_name="fixture",
                collector_version="1",
                parse_status="success",
                cumulative_sold=27,
                raw_payload={"sold": 27},
            ),
        ]
    )
    db_session.commit()
    calculate_product_daily_metrics(db_session, product.id)
    calculate_store_daily_metric(db_session, store.id, datetime(2026, 9, 19).date())
    return store, product


def test_product_metric_api_hides_legacy_cumulative_estimates(client, db_session):
    _, product = seed_metrics(db_session)
    response = client.get(f"/api/products/{product.id}/daily-metrics")
    assert response.status_code == 200
    assert response.json() == []
    assert db_session.query(ProductSnapshot).filter_by(product_id=product.id).count() == 2


def test_store_metric_api_hides_legacy_cumulative_estimates(client, db_session):
    store, product = seed_metrics(db_session)
    response = client.get(f"/api/stores/{store.id}/daily-metrics")
    assert response.status_code == 200
    assert response.json() == []
    assert db_session.query(ProductSnapshot).filter_by(product_id=product.id).count() == 2


def test_openapi_and_docs_are_available(client):
    schema = client.get("/openapi.json")
    assert schema.status_code == 200
    assert "/api/products/{product_id}/daily-metrics" in schema.json()["paths"]
    assert client.get("/docs").status_code == 200
