from datetime import datetime, timezone
from decimal import Decimal

from app.collectors.base import CollectorResult
from app.collectors.dependencies import get_collector
from app.db.models import CollectionAttempt, ManualCollectionTask, ProductSnapshot


def create_product(client, product_id="100500700"):
    store = client.post(
        "/api/stores",
        json={"name": "Channel Store", "url": "https://www.aliexpress.com/store/770700"},
    ).json()
    product = client.post(
        "/api/products",
        json={"store_id": store["id"], "aliexpress_product_id": product_id},
    ).json()
    return store, product


def extension_payload(product_id="100500700", sold_count=88):
    return {
        "platform_product_id": product_id,
        "url": f"https://www.aliexpress.com/item/{product_id}.html",
        "title": "Extension Product",
        "sold_count": sold_count,
        "price": "12.50",
        "rating": "4.8",
        "review_count": 321,
        "captured_at": "2026-09-21T10:00:00+08:00",
        "raw_data": {"extractor_version": "extension-test"},
    }


def test_browser_extension_creates_channel_snapshot_without_overwriting(client, db_session):
    _, product = create_product(client)

    first = client.post("/api/collection/browser-extension", json=extension_payload())
    second = client.post(
        "/api/collection/browser-extension",
        json=extension_payload(sold_count=91),
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["source"] == "CHROME_EXTENSION"
    assert first.json()["status"] == "VALID"
    assert first.json()["sold_count"] == 88
    assert second.json()["sold_count"] == 91
    snapshots = list(db_session.query(ProductSnapshot).filter_by(product_id=product["id"]))
    assert len(snapshots) == 2
    assert snapshots[0].raw_data == {"extractor_version": "extension-test"}


def test_browser_extension_rejects_unmonitored_product(client):
    response = client.post(
        "/api/collection/browser-extension",
        json=extension_payload(product_id="100500799"),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "该商品尚未加入监控"


def test_auto_failure_creates_attempt_and_manual_task(client, db_session):
    _, product = create_product(client, product_id="100500702")

    class FailedCollector:
        name = "failed_fixture"
        version = "1"

        def collect(self, target):
            return CollectorResult(
                collected_at=datetime(2026, 9, 21, 2, tzinfo=timezone.utc),
                collector_name=self.name,
                collector_version=self.version,
                parse_status="failed",
                error_type="platform_challenge",
                error_message="verification page",
            )

    client.app.dependency_overrides[get_collector] = lambda: FailedCollector()
    response = client.post("/api/jobs/collect-now")

    assert response.status_code == 200
    attempt = db_session.query(CollectionAttempt).filter_by(product_id=product["id"]).one()
    task = db_session.query(ManualCollectionTask).filter_by(product_id=product["id"]).one()
    assert attempt.source == "AUTO"
    assert attempt.status == "FAILED"
    assert task.status == "PENDING"
    assert client.get("/api/collection/pending").json()[0]["product_id"] == product["id"]


def test_manual_extension_completion_closes_pending_task(client, db_session):
    _, product = create_product(client, product_id="100500703")
    task = ManualCollectionTask(
        product_id=product["id"],
        reason="platform_challenge",
        status="PENDING",
    )
    db_session.add(task)
    db_session.commit()

    response = client.post(
        "/api/collection/browser-extension",
        json=extension_payload(product_id="100500703", sold_count=5),
    )

    assert response.status_code == 201
    db_session.refresh(task)
    assert task.status == "COMPLETED"
    assert client.get("/api/collection/pending").json() == []


def test_collection_status_today_exposes_auto_and_manual_counts(client):
    create_product(client, product_id="100500704")
    response = client.get("/api/collection/status/today")

    assert response.status_code == 200
    body = response.json()
    assert body["total_products"] == 1
    assert set(body) == {
        "date",
        "total_products",
        "auto_success",
        "auto_failed",
        "manual_completed",
        "pending_manual",
        "success_rate",
    }
