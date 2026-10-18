from app.db.models import Product, ProductSnapshot


def create_store(client, **overrides):
    payload = {
        "name": "Peer Store",
        "url": "https://www.aliexpress.com/store/110001",
    }
    payload.update(overrides)
    return client.post("/api/stores", json=payload)


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_store_crud_and_deactivate(client, db_session):
    created = create_store(client)
    assert created.status_code == 201
    assert created.json()["aliexpress_store_id"] == "110001"

    updated = client.patch(
        f"/api/stores/{created.json()['id']}", json={"name": "Updated Peer Store"}
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Updated Peer Store"

    product = client.post(
        "/api/products",
        json={
            "store_id": created.json()["id"],
            "aliexpress_product_id": "100500110001",
        },
    )
    assert product.status_code == 201
    snapshot = ProductSnapshot(
        product_id=product.json()["id"],
        collector_name="fixture",
        collector_version="1",
        parse_status="success",
        cumulative_sold=12,
        raw_payload={},
    )
    db_session.add(snapshot)
    db_session.commit()
    snapshot_id = snapshot.id

    deactivated = client.post(f"/api/stores/{created.json()['id']}/deactivate")
    assert deactivated.json()["status"] == "inactive"
    assert client.get("/api/stores?status=inactive").json()[0]["id"] == created.json()["id"]
    assert client.get("/api/stores?status=active").json() == []
    assert db_session.get(Product, product.json()["id"]).status == "active"
    assert db_session.get(ProductSnapshot, snapshot_id).cumulative_sold == 12


def test_clear_active_store_products_preserves_history_and_does_not_queue_collection(client, db_session):
    store = create_store(client).json()
    active_product = client.post(
        "/api/products",
        json={"store_id": store["id"], "aliexpress_product_id": "100500110099"},
    ).json()
    already_inactive = client.post(
        "/api/products",
        json={"store_id": store["id"], "aliexpress_product_id": "100500110098"},
    ).json()
    db_session.get(Product, already_inactive["id"]).status = "inactive"
    snapshot = ProductSnapshot(
        product_id=active_product["id"],
        collector_name="fixture",
        collector_version="1",
        parse_status="success",
        cumulative_sold=12,
        raw_payload={"kept": True},
    )
    db_session.add(snapshot)
    db_session.commit()

    rejected = client.post("/api/products/clear-active", json={"confirm_clear": False})
    assert rejected.status_code == 422
    response = client.post("/api/products/clear-active", json={"confirm_clear": True})

    assert response.status_code == 200, response.text
    assert response.json() == {"deactivated_count": 1}
    cleared = db_session.get(Product, active_product["id"])
    untouched = db_session.get(Product, already_inactive["id"])
    assert cleared.status == "inactive"
    assert cleared.pool_reset_reactivation_eligible is True
    assert untouched.status == "inactive"
    assert untouched.pool_reset_reactivation_eligible is False
    assert db_session.get(ProductSnapshot, snapshot.id).cumulative_sold == 12
    assert client.get("/api/browser-collection/runs/today").json() is None


def test_rejects_non_aliexpress_store_url(client):
    response = create_store(client, url="https://example.com/store/1")
    assert response.status_code == 422


def test_store_can_be_created_from_url_only(client):
    response = client.post(
        "/api/stores", json={"url": "https://www.aliexpress.com/store/110009"}
    )
    assert response.status_code == 201
    assert response.json()["name"] == "AliExpress Store 110009"


def test_duplicate_store_returns_conflict(client):
    assert create_store(client).status_code == 201
    assert create_store(client).status_code == 409


def test_product_crud_by_url_and_id(client):
    store_id = create_store(client).json()["id"]
    created = client.post(
        "/api/products",
        json={
            "store_id": store_id,
            "url": "https://www.aliexpress.com/item/100500123.html?spm=test",
            "title": "Observed product",
        },
    )
    assert created.status_code == 201
    assert created.json()["aliexpress_product_id"] == "100500123"

    by_id = client.post(
        "/api/products",
        json={"store_id": store_id, "aliexpress_product_id": "100500456"},
    )
    assert by_id.status_code == 201
    assert by_id.json()["url"].endswith("/100500456.html")

    deactivated = client.post(f"/api/products/{created.json()['id']}/deactivate")
    assert deactivated.json()["status"] == "inactive"
    assert len(client.get(f"/api/products?store_id={store_id}").json()) == 2


def test_product_titles_drop_public_sales_and_price_suffixes_before_storage(client, db_session):
    store_id = create_store(client).json()["id"]
    created = client.post(
        "/api/products",
        json={
            "store_id": store_id,
            "aliexpress_product_id": "100500123999",
            "title": "Desk Organizer 1,200 sold HK$ 52.32 New shoppers save HK$20",
        },
    )

    assert created.status_code == 201
    product_id = created.json()["id"]
    assert created.json()["title"] == "Desk Organizer"
    assert db_session.get(Product, product_id).title == "Desk Organizer"

    updated = client.patch(
        f"/api/products/{product_id}", json={"title": "Desk Organizer € 4.25"}
    )
    assert updated.status_code == 200
    assert updated.json()["title"] == "Desk Organizer"
    assert db_session.get(Product, product_id).title == "Desk Organizer"


def test_rejects_duplicate_and_mismatched_product(client):
    store_id = create_store(client).json()["id"]
    payload = {
        "store_id": store_id,
        "url": "https://www.aliexpress.com/item/100500123.html",
    }
    assert client.post("/api/products", json=payload).status_code == 201
    assert client.post("/api/products", json=payload).status_code == 409

    mismatch = client.post(
        "/api/products",
        json={**payload, "aliexpress_product_id": "999"},
    )
    assert mismatch.status_code == 422


def test_cannot_add_product_to_inactive_store(client):
    store_id = create_store(client).json()["id"]
    client.post(f"/api/stores/{store_id}/deactivate")
    response = client.post(
        "/api/products",
        json={"store_id": store_id, "aliexpress_product_id": "100500123"},
    )
    assert response.status_code == 409
