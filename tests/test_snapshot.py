from datetime import datetime, timezone
from decimal import Decimal

from app.collectors.base import CollectorResult
from app.collectors.dependencies import get_collector
from app.db.models import ProductSnapshot


class StubCollector:
    name = "stub"
    version = "test"

    def __init__(self, result: CollectorResult) -> None:
        self.result = result
        self.target = None

    def collect(self, target):
        self.target = target
        return self.result


def create_product(client):
    store = client.post(
        "/api/stores",
        json={"name": "Snapshot Store", "url": "https://www.aliexpress.com/store/220001"},
    ).json()
    return client.post(
        "/api/products",
        json={"store_id": store["id"], "aliexpress_product_id": "100500991"},
    ).json()


def result(**overrides):
    values = {
        "collected_at": datetime(2026, 9, 20, 1, tzinfo=timezone.utc),
        "collector_name": "fixture",
        "collector_version": "1.2",
        "parse_status": "success",
        "cumulative_sold": 44,
        "price_amount": Decimal("9.99"),
        "price_currency": "USD",
        "review_count": 8,
        "title": "Captured title",
        "source_http_status": 200,
        "raw_payload": {"source": {"sold": 44}},
        "raw_content": "<html>raw immutable input</html>",
    }
    values.update(overrides)
    return CollectorResult(**values)


def test_manual_collection_saves_raw_and_normalized_snapshot(client, db_session):
    product = create_product(client)
    client.app.dependency_overrides[get_collector] = lambda: StubCollector(result())

    response = client.post(f"/api/products/{product['id']}/collect")

    assert response.status_code == 200
    body = response.json()
    assert body["cumulative_sold"] == 44
    assert body["raw_payload"] == {"source": {"sold": 44}}
    assert body["raw_content"] == "<html>raw immutable input</html>"
    stored = db_session.get(ProductSnapshot, body["id"])
    assert stored.collector_version == "1.2"


def test_failed_collection_is_also_saved(client, db_session):
    product = create_product(client)
    failed = result(
        parse_status="failed",
        cumulative_sold=None,
        price_amount=None,
        error_type="page_structure_unrecognized",
        error_message="fixture changed",
        raw_payload={"response": "kept"},
    )
    client.app.dependency_overrides[get_collector] = lambda: StubCollector(failed)

    response = client.post(f"/api/products/{product['id']}/collect")

    assert response.status_code == 200
    assert response.json()["parse_status"] == "failed"
    stored = db_session.get(ProductSnapshot, response.json()["id"])
    assert stored.error_message == "fixture changed"
    assert stored.raw_payload == {"response": "kept"}


def test_new_snapshot_does_not_modify_previous_snapshot(client, db_session):
    product = create_product(client)
    client.app.dependency_overrides[get_collector] = lambda: StubCollector(result())
    first_id = client.post(f"/api/products/{product['id']}/collect").json()["id"]
    client.app.dependency_overrides[get_collector] = lambda: StubCollector(
        result(cumulative_sold=None, parse_status="failed", raw_content="changed page")
    )
    client.post(f"/api/products/{product['id']}/collect")

    first = db_session.get(ProductSnapshot, first_id)
    assert first.cumulative_sold == 44
    assert first.raw_content == "<html>raw immutable input</html>"
    assert len(client.get(f"/api/products/{product['id']}/snapshots").json()) == 2


def test_collection_uses_canonical_product_url(client):
    store = client.post(
        "/api/stores",
        json={"name": "Canonical Store", "url": "https://www.aliexpress.com/store/220002"},
    ).json()
    product = client.post(
        "/api/products",
        json={
            "store_id": store["id"],
            "url": "https://www.aliexpress.com/item/100500992.html?spm=tracking&gatewayAdapt=4itemAdapt",
        },
    ).json()
    collector = StubCollector(result())
    client.app.dependency_overrides[get_collector] = lambda: collector

    client.post(f"/api/products/{product['id']}/collect")

    assert collector.target.url == "https://www.aliexpress.com/item/100500992.html"
