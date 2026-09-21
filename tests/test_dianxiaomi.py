from app.db.models import DianxiaomiHandoff


def create_store_product(client, product_id="100500880"):
    store = client.post(
        "/api/stores",
        json={"name": "店小秘测试店", "url": "https://www.aliexpress.com/store/770880"},
    ).json()
    product = client.post(
        "/api/products",
        json={"store_id": store["id"], "aliexpress_product_id": product_id},
    ).json()
    return store, product


def test_dianxiaomi_batch_deduplicates_and_claims_next(client, db_session):
    _, product = create_store_product(client)
    created = client.post(
        "/api/integrations/dianxiaomi/handoffs",
        json={"product_ids": [product["id"], product["id"]]},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["total_count"] == 1
    assert body["queued_count"] == 1
    assert body["reused_count"] == 0

    repeated = client.post(
        "/api/integrations/dianxiaomi/handoffs",
        json={"product_ids": [product["id"]]},
    )
    assert repeated.status_code == 201
    assert repeated.json()["reused_count"] == 1
    assert db_session.query(DianxiaomiHandoff).count() == 1

    claimed = client.post(
        "/api/integrations/dianxiaomi/handoffs/claim-next",
        json={"worker_id": "test-extension"},
    )
    assert claimed.status_code == 200
    assert claimed.json()["status"] == "CLAIMED"
    assert claimed.json()["product_id"] == product["id"]


def test_dianxiaomi_claim_batch_returns_multiple_urls(client):
    store, first = create_store_product(client, product_id="100500884")
    second = client.post(
        "/api/products",
        json={"store_id": store["id"], "aliexpress_product_id": "100500885"},
    ).json()
    client.post(
        "/api/integrations/dianxiaomi/handoffs",
        json={"product_ids": [first["id"], second["id"]]},
    )
    claimed = client.post(
        "/api/integrations/dianxiaomi/handoffs/claim-batch",
        json={"worker_id": "test-extension", "limit": 20},
    )
    assert claimed.status_code == 200
    body = claimed.json()
    assert len(body) == 2
    assert {item["status"] for item in body} == {"CLAIMED"}
    assert {item["store_name"] for item in body} == {store["name"]}


def test_dianxiaomi_handoff_status_and_summary(client, db_session):
    _, product = create_store_product(client, product_id="100500881")
    created = client.post(
        "/api/integrations/dianxiaomi/handoffs",
        json={"product_ids": [product["id"]]},
    ).json()
    handoff_id = created["items"][0]["id"]
    claimed = client.post(
        f"/api/integrations/dianxiaomi/handoffs/{handoff_id}/claim",
        json={"worker_id": "test-extension"},
    )
    assert claimed.status_code == 200
    updated = client.post(
        f"/api/integrations/dianxiaomi/handoffs/{handoff_id}/status",
        json={"status": "COLLECTING", "worker_id": "test-extension"},
    )
    assert updated.status_code == 200
    assert updated.json()["status"] == "COLLECTING"
    summary = client.get("/api/integrations/dianxiaomi/status")
    assert summary.status_code == 200
    assert summary.json()["processing"] == 1
    assert summary.json()["items"][0]["store_name"] == "店小秘测试店"


def test_dianxiaomi_only_accepts_active_monitored_products(client):
    _, product = create_store_product(client, product_id="100500882")
    assert client.post(f"/api/products/{product['id']}/deactivate").status_code == 200
    response = client.post(
        "/api/integrations/dianxiaomi/handoffs",
        json={"product_ids": [product["id"]]},
    )
    assert response.status_code == 422


def test_dianxiaomi_manual_confirmation_can_resume(client):
    _, product = create_store_product(client, product_id="100500883")
    created = client.post(
        "/api/integrations/dianxiaomi/handoffs",
        json={"product_ids": [product["id"]]},
    ).json()
    handoff_id = created["items"][0]["id"]
    client.post(
        f"/api/integrations/dianxiaomi/handoffs/{handoff_id}/claim",
        json={"worker_id": "test-extension"},
    )
    client.post(
        f"/api/integrations/dianxiaomi/handoffs/{handoff_id}/status",
        json={
            "status": "NEEDS_CONFIRMATION",
            "worker_id": "test-extension",
            "error_type": "manual_confirmation_required",
            "error_message": "请确认协议",
        },
    )
    resumed = client.post(f"/api/integrations/dianxiaomi/handoffs/{handoff_id}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "QUEUED"
