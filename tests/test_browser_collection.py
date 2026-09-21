from datetime import date, datetime, timedelta, timezone

from app.db.models import (
    BrowserCollectionItem,
    BrowserCollectionRun,
    Product,
    ProductDailyMetric,
    ProductSnapshot,
)


def create_store(client, store_id="990100"):
    return client.post(
        "/api/stores",
        json={"name": "Scheduled Browser Store", "url": f"https://www.aliexpress.com/store/{store_id}"},
    ).json()


def create_product(client, store_id, product_id="100500990001"):
    return client.post(
        "/api/products",
        json={"store_id": store_id, "aliexpress_product_id": product_id},
    ).json()


def store_payload(store_id="990100", count=20):
    return {
        "platform_store_id": store_id,
        "url": f"https://www.aliexpress.com/store/{store_id}/pages/all-items.html?shop_sortType=orders_desc",
        "products": [
            {
                "platform_product_id": f"100500991{index:04d}",
                "url": f"https://www.aliexpress.com/item/100500991{index:04d}.html",
                "title": f"Scheduled Product {index}",
                "public_cumulative_sold": 1000 - index,
            }
            for index in range(count)
        ],
        "raw_data": {"extractor_version": "scheduled-test"},
    }


def product_payload(product_id, sold_count=120):
    return {
        "platform_product_id": product_id,
        "url": f"https://www.aliexpress.com/item/{product_id}.html",
        "title": "Scheduled Product",
        "sold_count": sold_count,
        "price": "9.90",
        "rating": "4.7",
        "review_count": 44,
        "captured_at": "2026-09-21T10:00:00+08:00",
        "raw_data": {"extractor_version": "scheduled-test"},
    }


def test_ensure_daily_run_is_idempotent_and_starts_with_store_tasks(client, db_session):
    store = create_store(client)
    response = client.post(
        "/api/browser-collection/runs/ensure",
        json={"target_date": "2026-09-21", "trigger": "MANUAL"},
    )
    again = client.post(
        "/api/browser-collection/runs/ensure",
        json={"target_date": "2026-09-21", "trigger": "MANUAL"},
    )

    assert response.status_code == 200
    assert again.status_code == 200
    assert response.json()["id"] == again.json()["id"]
    assert response.json()["phase"] == "STORE_DISCOVERY"
    items = db_session.query(BrowserCollectionItem).all()
    assert len(items) == 1
    assert items[0].target_type == "STORE"
    assert items[0].store_id == store["id"]


def test_store_task_creates_store_snapshots_without_product_tasks(client, db_session):
    store = create_store(client, "990101")
    client.post(
        "/api/browser-collection/runs/ensure",
        json={"target_date": "2026-09-21", "trigger": "MANUAL"},
    )
    claim = client.post("/api/browser-collection/items/claim")
    assert claim.json()["item"]["target_type"] == "STORE"

    result = client.post(
        f"/api/browser-collection/items/{claim.json()['item']['id']}/store-discovery",
        json=store_payload(store_id="990101"),
    )
    assert result.status_code == 200
    assert result.json()["phase"] == "COMPLETED"
    products = db_session.query(Product).filter_by(store_id=store["id"], status="active").all()
    assert len(products) == 20
    product_items = db_session.query(BrowserCollectionItem).filter_by(target_type="PRODUCT").all()
    assert len(product_items) == 0
    snapshots = db_session.query(ProductSnapshot).filter(ProductSnapshot.source == "CHROME_EXTENSION").all()
    assert len(snapshots) == 20
    assert all(snapshot.status == "VALID" for snapshot in snapshots)


def test_product_task_snapshot_is_atomic_and_keeps_raw_data(client, db_session):
    store = create_store(client, "990102")
    product = create_product(client, store["id"], "100500992001")
    client.post(
        "/api/browser-collection/runs/ensure",
        json={"target_date": "2026-09-21", "trigger": "MANUAL"},
    )
    # This run begins with a store task; completing it queues the pre-existing product.
    store_claim = client.post("/api/browser-collection/items/claim").json()["item"]
    discovery = store_payload(store_id="990102", count=1)
    discovery["products"][0] = {
        "platform_product_id": "100500992001",
        "url": "https://www.aliexpress.com/item/100500992001.html",
        "title": "Scheduled Product",
            "public_cumulative_sold": None,
    }
    client.post(
        f"/api/browser-collection/items/{store_claim['id']}/store-discovery",
        json=discovery,
    )
    # The pre-existing product is preserved as a manual product and remains queued.
    claim = client.post("/api/browser-collection/items/claim").json()["item"]
    assert claim["product_id"] == product["id"]

    response = client.post(
        f"/api/browser-collection/items/{claim['id']}/snapshot",
        json=product_payload("100500992001", 121),
    )
    assert response.status_code == 200
    assert response.json()["succeeded_count"] == 2
    snapshot = (
        db_session.query(ProductSnapshot)
        .filter_by(
            product_id=product["id"],
            source="CHROME_EXTENSION",
            parse_status="success",
        )
        .one()
    )
    assert snapshot.source == "CHROME_EXTENSION"
    assert snapshot.raw_data["collection_mode"] == "scheduled"
    assert snapshot.sold_count == 121
    assert db_session.get(BrowserCollectionItem, claim["id"]).status == "SUCCEEDED"


def test_challenge_pauses_and_resume_releases_the_item(client):
    store = create_store(client, "990103")
    create_product(client, store["id"], "100500993001")
    client.post(
        "/api/browser-collection/runs/ensure",
        json={"target_date": "2026-09-21", "trigger": "MANUAL"},
    )
    claim = client.post("/api/browser-collection/items/claim").json()["item"]
    paused = client.post(
        f"/api/browser-collection/items/{claim['id']}/challenge",
        json={"error_message": "verification page", "page_url": "https://www.aliexpress.com/punish"},
    )
    assert paused.status_code == 200
    assert paused.json()["status"] == "NEEDS_VERIFICATION"

    resumed = client.post(f"/api/browser-collection/runs/{paused.json()['id']}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "RUNNING"
    assert resumed.json()["current_item"] is None


def test_completing_challenged_page_clears_pause_reason(client):
    store = create_store(client, "990105")
    client.post(
        "/api/browser-collection/runs/ensure",
        json={"target_date": "2026-09-21", "trigger": "MANUAL"},
    )
    claim = client.post("/api/browser-collection/items/claim").json()["item"]
    paused = client.post(
        f"/api/browser-collection/items/{claim['id']}/challenge",
        json={"error_message": "verification page"},
    )
    assert paused.json()["paused_reason"] == "platform_challenge"

    completed = client.post(
        f"/api/browser-collection/items/{claim['id']}/store-discovery",
        json={
            **store_payload(store_id="990105", count=1),
            "products": [
                {
                    **store_payload(store_id="990105", count=1)["products"][0],
                    "public_cumulative_sold": None,
                }
            ],
        },
    )

    assert completed.status_code == 200
    assert completed.json()["status"] == "RUNNING"
    assert completed.json()["paused_reason"] is None
    assert completed.json()["succeeded_count"] == 1


def test_expired_lease_can_be_reclaimed(client, db_session):
    store = create_store(client, "990104")
    create_product(client, store["id"], "100500994001")
    client.post(
        "/api/browser-collection/runs/ensure",
        json={"target_date": "2026-09-21", "trigger": "MANUAL"},
    )
    first = client.post("/api/browser-collection/items/claim").json()["item"]
    item = db_session.get(BrowserCollectionItem, first["id"])
    item.lease_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.commit()

    second = client.post("/api/browser-collection/items/claim").json()["item"]
    assert second["id"] == first["id"]
    assert second["status"] == "RUNNING"
    assert second["attempt_count"] == 2
