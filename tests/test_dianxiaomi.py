from datetime import timedelta
from datetime import datetime, timezone

import pytest

from app.api.service_auth import require_dianxiaomi_service_token
from app.db.models import (
    BrowserCollectionControl,
    BrowserCollectionWorker,
    DianxiaomiHandoff,
    DianxiaomiQueueControl,
    Product,
)


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


def register_dianxiaomi_worker(
    db_session, worker_id, *, online=True, selected=True
):
    now = datetime.now(timezone.utc)
    heartbeat = now if online else now - timedelta(minutes=3)
    db_session.add(
        BrowserCollectionWorker(
            worker_id=worker_id,
            label=worker_id,
            last_heartbeat_at=heartbeat,
        )
    )
    control = db_session.get(BrowserCollectionControl, 1)
    if control is None:
        control = BrowserCollectionControl(id=1)
        db_session.add(control)
    if selected:
        control.selected_worker_id = worker_id
        control.selected_worker_label = worker_id
    db_session.commit()


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

    register_dianxiaomi_worker(db_session, "test-extension")
    claimed = client.post(
        "/api/integrations/dianxiaomi/handoffs/claim-next",
        json={"worker_id": "test-extension"},
    )
    assert claimed.status_code == 200
    assert claimed.json()["status"] == "CLAIMED"
    assert claimed.json()["product_id"] == product["id"]


def test_dianxiaomi_claim_batch_returns_multiple_urls(client, db_session):
    store, first = create_store_product(client, product_id="100500884")
    second = client.post(
        "/api/products",
        json={"store_id": store["id"], "aliexpress_product_id": "100500885"},
    ).json()
    client.post(
        "/api/integrations/dianxiaomi/handoffs",
        json={"product_ids": [first["id"], second["id"]]},
    )
    register_dianxiaomi_worker(db_session, "test-extension")
    claimed = client.post(
        "/api/integrations/dianxiaomi/handoffs/claim-batch",
        json={"worker_id": "test-extension", "limit": 20},
    )
    assert claimed.status_code == 200
    body = claimed.json()
    assert len(body) == 2
    assert {item["status"] for item in body} == {"CLAIMED"}
    assert {item["store_name"] for item in body} == {store["name"]}


def test_dianxiaomi_reclaims_stale_opened_handoff(client, db_session):
    _, product = create_store_product(client, product_id="100500886")
    created = client.post(
        "/api/integrations/dianxiaomi/handoffs",
        json={"product_ids": [product["id"]]},
    ).json()
    handoff = db_session.get(DianxiaomiHandoff, created["items"][0]["id"])
    handoff.status = "OPENED"
    handoff.worker_id = "old-extension"
    handoff.claimed_at = datetime.now(timezone.utc) - timedelta(minutes=11)
    db_session.commit()
    register_dianxiaomi_worker(db_session, "new-extension")

    claimed = client.post(
        "/api/integrations/dianxiaomi/handoffs/claim-batch",
        json={"worker_id": "new-extension", "limit": 20},
    )
    assert claimed.status_code == 200
    assert claimed.json()[0]["status"] == "CLAIMED"
    assert claimed.json()[0]["worker_id"] == "new-extension"


def test_dianxiaomi_handoff_status_and_summary(client, db_session):
    _, product = create_store_product(client, product_id="100500881")
    created = client.post(
        "/api/integrations/dianxiaomi/handoffs",
        json={"product_ids": [product["id"]]},
    ).json()
    handoff_id = created["items"][0]["id"]
    register_dianxiaomi_worker(db_session, "test-extension")
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


def test_dianxiaomi_only_accepts_active_monitored_products(client, db_session):
    _, product = create_store_product(client, product_id="100500882")
    db_session.get(Product, product["id"]).status = "inactive"
    db_session.commit()
    response = client.post(
        "/api/integrations/dianxiaomi/handoffs",
        json={"product_ids": [product["id"]]},
    )
    assert response.status_code == 422


def test_dianxiaomi_manual_confirmation_can_resume(client, db_session):
    _, product = create_store_product(client, product_id="100500883")
    created = client.post(
        "/api/integrations/dianxiaomi/handoffs",
        json={"product_ids": [product["id"]]},
    ).json()
    handoff_id = created["items"][0]["id"]
    register_dianxiaomi_worker(db_session, "test-extension")
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


def test_dianxiaomi_product_page_mismatch_does_not_pause_queue(client, db_session):
    _, product = create_store_product(client, product_id="100500896")
    created = client.post(
        "/api/integrations/dianxiaomi/handoffs",
        json={"product_ids": [product["id"]]},
    ).json()
    handoff_id = created["items"][0]["id"]
    worker_id = "qx-mismatch-worker"
    register_dianxiaomi_worker(db_session, worker_id)
    claimed = client.post(
        f"/api/integrations/dianxiaomi/handoffs/{handoff_id}/claim",
        json={"worker_id": worker_id},
    )
    assert claimed.status_code == 200

    reviewed = client.post(
        f"/api/integrations/dianxiaomi/handoffs/{handoff_id}/status",
        json={
            "status": "NEEDS_CONFIRMATION",
            "worker_id": worker_id,
            "error_type": "manual_confirmation_required",
            "error_message": (
                "店小秘工作窗口未加载队列商品（目标商品 ID 100500896，当前商品 ID 200500896）"
            ),
        },
    )

    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "NEEDS_CONFIRMATION"
    assert reviewed.json()["error_type"] == "product_page_mismatch"
    control = db_session.get(DianxiaomiQueueControl, 1)
    assert control is None or control.is_paused is False

    confirmed = client.post(
        f"/api/integrations/dianxiaomi/handoffs/{handoff_id}/identity-review/confirm"
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "QUEUED"
    assert confirmed.json()["target_url"] == "https://www.aliexpress.com/item/200500896.html"


def test_dianxiaomi_product_identity_confirmation_requeues_and_persists_redirect(client, db_session):
    target_id = "3256812865076992"
    actual_id = "1005013051391744"
    _, product = create_store_product(client, product_id=target_id)
    created = client.post(
        "/api/integrations/dianxiaomi/handoffs",
        json={"product_ids": [product["id"]]},
    ).json()
    handoff = db_session.get(DianxiaomiHandoff, created["items"][0]["id"])
    handoff.status = "NEEDS_CONFIRMATION"
    handoff.error_type = "manual_confirmation_required"
    handoff.error_message = (
        f"店小秘工作窗口未加载队列商品（目标商品 ID {target_id}，当前商品 ID {actual_id}）"
    )
    db_session.commit()

    confirmed = client.post(
        f"/api/integrations/dianxiaomi/handoffs/{handoff.id}/identity-review/confirm"
    )

    assert confirmed.status_code == 200
    body = confirmed.json()
    approved_url = f"https://www.aliexpress.com/item/{actual_id}.html"
    assert body["status"] == "QUEUED"
    assert body["target_url"] == approved_url
    assert body["error_type"] is None
    assert body["detail"]["approved_product_page"] == {
        "monitored_product_id": target_id,
        "actual_product_id": actual_id,
        "url": approved_url,
        "source_handoff_id": handoff.id,
        "approved_at": body["detail"]["approved_product_page"]["approved_at"],
    }

    handoff.status = "SUCCEEDED"
    db_session.commit()
    next_batch = client.post(
        "/api/integrations/dianxiaomi/handoffs",
        json={"product_ids": [product["id"]]},
    )
    assert next_batch.status_code == 201
    assert next_batch.json()["items"][0]["target_url"] == approved_url


def test_dianxiaomi_product_identity_confirmation_uses_reviewed_actual_url(client, db_session):
    target_id = "3256810000000001"
    actual_id = "1005010000000001"
    _, product = create_store_product(client, product_id=target_id)
    created = client.post(
        "/api/integrations/dianxiaomi/handoffs",
        json={"product_ids": [product["id"]]},
    ).json()
    handoff = db_session.get(DianxiaomiHandoff, created["items"][0]["id"])
    handoff.status = "NEEDS_CONFIRMATION"
    handoff.error_type = "manual_confirmation_required"
    handoff.error_message = (
        f"店小秘工作窗口未加载队列商品（目标商品 ID {target_id}，当前商品 ID {actual_id}）"
    )
    handoff.detail_json = {
        "product_page_mismatch": {
            "expected_product_id": target_id,
            "actual_product_id": actual_id,
            "actual_url": f"https://es.aliexpress.com/item/{actual_id}.html?spm=verified",
        }
    }
    db_session.commit()

    confirmed = client.post(
        f"/api/integrations/dianxiaomi/handoffs/{handoff.id}/identity-review/confirm"
    )

    assert confirmed.status_code == 200
    assert confirmed.json()["target_url"] == (
        f"https://es.aliexpress.com/item/{actual_id}.html?spm=verified"
    )


def test_dianxiaomi_product_identity_rejection_cancels_without_recording_alias(client, db_session):
    _, product = create_store_product(client, product_id="3256810000000002")
    created = client.post(
        "/api/integrations/dianxiaomi/handoffs",
        json={"product_ids": [product["id"]]},
    ).json()
    handoff_id = created["items"][0]["id"]
    handoff = db_session.get(DianxiaomiHandoff, handoff_id)
    handoff.status = "NEEDS_CONFIRMATION"
    handoff.error_type = "manual_confirmation_required"
    handoff.error_message = (
        "店小秘工作窗口未加载队列商品（目标商品 ID 3256810000000002，当前商品 ID 1005010000000002）"
    )
    db_session.commit()

    rejected = client.post(
        f"/api/integrations/dianxiaomi/handoffs/{handoff_id}/identity-review/reject"
    )

    assert rejected.status_code == 200
    assert rejected.json()["status"] == "CANCELED"
    assert rejected.json()["detail"] is None


def test_dianxiaomi_product_page_mismatch_cannot_be_requeued_without_review(client, db_session):
    _, product = create_store_product(client, product_id="3256810000000003")
    created = client.post(
        "/api/integrations/dianxiaomi/handoffs",
        json={"product_ids": [product["id"]]},
    ).json()
    handoff_id = created["items"][0]["id"]
    handoff = db_session.get(DianxiaomiHandoff, handoff_id)
    handoff.status = "NEEDS_CONFIRMATION"
    handoff.error_type = "manual_confirmation_required"
    handoff.error_message = (
        "店小秘工作窗口未加载队列商品（目标商品 ID 3256810000000003，当前商品 ID 1005010000000003）"
    )
    db_session.commit()

    response = client.post(f"/api/integrations/dianxiaomi/handoffs/{handoff_id}/resume")

    assert response.status_code == 409
    assert db_session.get(DianxiaomiHandoff, handoff_id).status == "NEEDS_CONFIRMATION"


def test_dianxiaomi_worker_resumes_its_paused_batch_after_page_is_ready(client, db_session):
    store, first = create_store_product(client, product_id="100500887")
    second = client.post(
        "/api/products",
        json={"store_id": store["id"], "aliexpress_product_id": "100500888"},
    ).json()
    created = client.post(
        "/api/integrations/dianxiaomi/handoffs",
        json={"product_ids": [first["id"], second["id"]]},
    ).json()
    register_dianxiaomi_worker(db_session, "isolated-worker")
    claimed = client.post(
        "/api/integrations/dianxiaomi/handoffs/claim-batch",
        json={"worker_id": "isolated-worker", "limit": 20},
    )
    assert {item["status"] for item in claimed.json()} == {"CLAIMED"}
    for item in claimed.json():
        response = client.post(
            f"/api/integrations/dianxiaomi/handoffs/{item['id']}/status",
            json={
                "status": "NEEDS_CONFIRMATION",
                "worker_id": "isolated-worker",
                "error_message": "请先勾选店小秘采集协议",
            },
        )
        assert response.status_code == 200

    resumed = client.post(
        "/api/integrations/dianxiaomi/handoffs/claim-batch",
        json={"worker_id": "isolated-worker", "limit": 20, "resume_confirmed": True},
    )
    assert resumed.status_code == 200
    assert len(resumed.json()) == 2
    assert {item["status"] for item in resumed.json()} == {"CLAIMED"}


@pytest.mark.parametrize(
    ("selected_worker_id", "online", "request_worker_id"),
    [
        (None, True, "worker-a"),
        ("worker-b", True, "worker-a"),
        ("worker-a", False, "worker-a"),
    ],
)
def test_dianxiaomi_claim_requires_selected_online_worker(
    client, db_session, selected_worker_id, online, request_worker_id
):
    _, product = create_store_product(client, product_id="100500889")
    created = client.post(
        "/api/integrations/dianxiaomi/handoffs",
        json={"product_ids": [product["id"]]},
    ).json()
    if selected_worker_id:
        register_dianxiaomi_worker(
            db_session,
            selected_worker_id,
            online=online,
        )
    if request_worker_id != selected_worker_id:
        register_dianxiaomi_worker(db_session, request_worker_id, selected=False)

    claim = client.post(
        "/api/integrations/dianxiaomi/handoffs/claim-batch",
        json={"worker_id": request_worker_id, "limit": 20},
    )

    assert claim.status_code == 200
    assert claim.json() == []
    handoff = db_session.get(DianxiaomiHandoff, created["items"][0]["id"])
    assert handoff.status == "QUEUED"


def test_dianxiaomi_claim_waits_for_other_workers_active_handoff(client, db_session):
    store, first = create_store_product(client, product_id="100500890")
    second = client.post(
        "/api/products",
        json={"store_id": store["id"], "aliexpress_product_id": "100500891"},
    ).json()
    created = client.post(
        "/api/integrations/dianxiaomi/handoffs",
        json={"product_ids": [first["id"], second["id"]]},
    ).json()
    old_owner = "galore-worker"
    new_owner = "qx-worker"
    register_dianxiaomi_worker(db_session, old_owner)
    active = db_session.get(DianxiaomiHandoff, created["items"][0]["id"])
    active.status = "OPENED"
    active.worker_id = old_owner
    active.claimed_at = datetime.now(timezone.utc)
    db_session.commit()
    register_dianxiaomi_worker(db_session, new_owner)

    claim = client.post(
        "/api/integrations/dianxiaomi/handoffs/claim-batch",
        json={"worker_id": new_owner, "limit": 20},
    )

    assert claim.status_code == 200
    assert claim.json() == []
    queued = db_session.get(DianxiaomiHandoff, created["items"][1]["id"])
    assert queued.status == "QUEUED"


def test_dianxiaomi_status_rejects_stale_owner_after_requeue(client, db_session):
    _, product = create_store_product(client, product_id="100500892")
    created = client.post(
        "/api/integrations/dianxiaomi/handoffs",
        json={"product_ids": [product["id"]]},
    ).json()
    handoff_id = created["items"][0]["id"]
    owner = "qx-worker"
    register_dianxiaomi_worker(db_session, owner)
    claim = client.post(
        f"/api/integrations/dianxiaomi/handoffs/{handoff_id}/claim",
        json={"worker_id": owner},
    )
    assert claim.status_code == 200
    failed = client.post(
        f"/api/integrations/dianxiaomi/handoffs/{handoff_id}/status",
        json={"status": "FAILED", "worker_id": owner},
    )
    assert failed.status_code == 200
    assert client.post(f"/api/integrations/dianxiaomi/handoffs/{handoff_id}/resume").status_code == 200

    late_update = client.post(
        f"/api/integrations/dianxiaomi/handoffs/{handoff_id}/status",
        json={"status": "SUCCEEDED", "worker_id": owner},
    )

    assert late_update.status_code == 409
    assert db_session.get(DianxiaomiHandoff, handoff_id).status == "QUEUED"


def test_dianxiaomi_docker_claim_obeys_selected_owner(client, db_session):
    from app.main import app

    _, product = create_store_product(client, product_id="100500893")
    client.post(
        "/api/integrations/dianxiaomi/handoffs",
        json={"product_ids": [product["id"]]},
    )
    register_dianxiaomi_worker(db_session, "qx-worker")
    app.dependency_overrides[require_dianxiaomi_service_token] = lambda: None
    try:
        claim = client.post(
            "/api/integrations/dianxiaomi/worker/handoffs/claim",
            headers={"X-Dianxiaomi-Worker-ID": "docker-worker"},
        )
    finally:
        app.dependency_overrides.pop(require_dianxiaomi_service_token, None)

    assert claim.status_code == 200
    assert claim.json() is None


def test_dianxiaomi_status_reports_selected_worker_and_active_owner(client, db_session):
    store, first = create_store_product(client, product_id="100500894")
    second = client.post(
        "/api/products",
        json={"store_id": store["id"], "aliexpress_product_id": "100500895"},
    ).json()
    created = client.post(
        "/api/integrations/dianxiaomi/handoffs",
        json={"product_ids": [first["id"], second["id"]]},
    ).json()
    selected_worker_id = "qx-worker"
    active_owner_id = "galore-worker"
    register_dianxiaomi_worker(db_session, selected_worker_id)
    register_dianxiaomi_worker(db_session, active_owner_id, selected=False)
    active = db_session.get(DianxiaomiHandoff, created["items"][0]["id"])
    active.status = "OPENED"
    active.worker_id = active_owner_id
    active.claimed_at = datetime.now(timezone.utc)
    db_session.commit()

    status = client.get("/api/integrations/dianxiaomi/status")

    assert status.status_code == 200
    body = status.json()
    assert body["selected_worker"] == {
        "worker_id": selected_worker_id,
        "label": selected_worker_id,
        "online": True,
    }
    assert body["worker_online"] is True
    assert body["active_owner"] == {
        "worker_id": active_owner_id,
        "label": active_owner_id,
    }
    assert body["waiting_for_owner"] is True
    assert "完成在途任务" in body["waiting_reason"]
