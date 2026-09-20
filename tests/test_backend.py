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


def test_store_crud_and_deactivate(client):
    created = create_store(client)
    assert created.status_code == 201
    assert created.json()["aliexpress_store_id"] == "110001"

    updated = client.patch(
        f"/api/stores/{created.json()['id']}", json={"name": "Updated Peer Store"}
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Updated Peer Store"

    deactivated = client.post(f"/api/stores/{created.json()['id']}/deactivate")
    assert deactivated.json()["status"] == "inactive"
    assert client.get("/api/stores?status=inactive").json()[0]["id"] == created.json()["id"]


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
