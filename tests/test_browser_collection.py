from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pytest

from app.db.models import (
    BrowserCollectionControl,
    BrowserCollectionItem,
    BrowserCollectionRun,
    BrowserCollectionWorker,
    Product,
    ProductDailyMetric,
    ProductSnapshot,
    Store,
    StorePageDiscoveryState,
    StoreSnapshot,
    StoreUrlIdentity,
)
from app.services.browser_collection import (
    _products_with_recent_history_for_date,
    serialize_run,
)
from app.services.metrics import calculate_product_daily_metrics
from app.services.store_page_discovery import (
    advance_store_page_cursor,
    get_or_create_store_page_discovery_state,
)

_last_claim_credentials = None


@pytest.fixture(autouse=True)
def reset_claim_credentials():
    global _last_claim_credentials
    _last_claim_credentials = None
    yield


def add_claim_credentials(payload, item_id=None):
    if _last_claim_credentials and (item_id is None or item_id == _last_claim_credentials["item_id"]):
        payload.update(
            worker_id=_last_claim_credentials["worker_id"],
            claim_token=_last_claim_credentials["claim_token"],
        )
    return payload


def register_browser_worker(client, worker_id="worker-galore", label="Galore主机"):
    return client.post(
        "/api/browser-collection/heartbeat",
        json={"worker_id": worker_id, "worker_label": label, "extension_version": "test"},
    )


def select_browser_worker(client, worker_id="worker-galore", confirm_handoff=False):
    return client.post(
        "/api/browser-collection/control/worker",
        json={"worker_id": worker_id, "confirm_handoff": confirm_handoff},
    )


def claim_item(client, worker_id="worker-test"):
    registered = register_browser_worker(client, worker_id, "测试采集电脑")
    assert registered.status_code == 200, registered.text
    control = client.get("/api/browser-collection/control")
    assert control.status_code == 200, control.text
    selected = control.json()["selected_worker"]
    if not selected or selected["worker_id"] != worker_id:
        response = select_browser_worker(client, worker_id, confirm_handoff=True)
        assert response.status_code == 200, response.text
    started = client.post("/api/browser-collection/runs/start-today")
    assert started.status_code == 200, started.text
    response = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": worker_id}
    )
    global _last_claim_credentials
    body = response.json()
    if body.get("item") and body.get("claim_token"):
        _last_claim_credentials = {
            "item_id": body["item"]["id"],
            "worker_id": worker_id,
            "claim_token": body["claim_token"],
        }
    return response


def request_claim(client, worker_id="worker-test"):
    response = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": worker_id}
    )
    body = response.json()
    if body.get("item") and body.get("claim_token"):
        global _last_claim_credentials
        _last_claim_credentials = {
            "item_id": body["item"]["id"],
            "worker_id": worker_id,
            "claim_token": body["claim_token"],
        }
    return response


def test_scheduled_run_is_created_held(client):
    store = create_store(client, "990201")
    create_product(client, store["id"], "1005009920101")

    response = client.post(
        "/api/browser-collection/runs/ensure", json={"trigger": "SCHEDULED"}
    )

    assert response.status_code == 200
    assert response.json()["dispatch_enabled"] is False
    assert response.json()["pending_count"] == 1


def test_run_roles_are_serialized_from_saved_ids_not_global_selection(db_session):
    primary = BrowserCollectionWorker(
        worker_id="run-primary",
        label="Primary",
        last_heartbeat_at=datetime.now(timezone.utc),
    )
    helpers = [
        BrowserCollectionWorker(
            worker_id=worker_id,
            label=worker_id,
            last_heartbeat_at=datetime.now(timezone.utc),
        )
        for worker_id in ("run-helper-b", "run-helper-a")
    ]
    next_primary = BrowserCollectionWorker(
        worker_id="next-run-primary",
        label="Next primary",
        last_heartbeat_at=datetime.now(timezone.utc),
    )
    run = BrowserCollectionRun(
        target_date=date.today(),
    )
    assert hasattr(run, "primary_worker_id")
    assert hasattr(run, "helper_workers")
    run.primary_worker_id = primary.worker_id
    run.helper_workers = helpers
    control = BrowserCollectionControl(
        id=1,
        selected_worker_id=next_primary.worker_id,
        selected_worker_label=next_primary.label,
    )
    db_session.add_all([primary, *helpers, next_primary, run, control])
    db_session.commit()
    db_session.expire_all()

    saved_run = db_session.get(BrowserCollectionRun, run.id)
    payload = serialize_run(saved_run)

    assert payload["primary_worker_id"] == "run-primary"
    assert payload["helper_worker_ids"] == ["run-helper-a", "run-helper-b"]


def test_held_run_rejects_legacy_and_unselected_claims(client):
    store = create_store(client, "990202")
    create_product(client, store["id"], "1005009920201")
    client.post("/api/browser-collection/runs/ensure", json={"trigger": "MANUAL"})
    register_browser_worker(client)

    legacy = client.post("/api/browser-collection/items/claim", json={})
    unselected = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-other"}
    )

    assert legacy.status_code == 200
    assert legacy.json()["item"] is None
    assert unselected.status_code == 200
    assert unselected.json()["item"] is None


def test_start_requires_selected_online_worker(client, monkeypatch):
    from app.services import browser_collection as browser_service

    no_owner = client.post("/api/browser-collection/runs/start-today")
    assert no_owner.status_code == 409

    register_browser_worker(client)
    no_selection = client.post("/api/browser-collection/runs/start-today")
    assert no_selection.status_code == 409

    assert select_browser_worker(client).status_code == 200
    stale_now = datetime.now(timezone.utc) - timedelta(minutes=3)
    monkeypatch.setattr(browser_service, "utcnow", lambda: stale_now)
    register_browser_worker(client)
    offline = client.post("/api/browser-collection/runs/start-today")

    assert offline.status_code == 409


@pytest.mark.parametrize(
    "helper_worker_ids",
    [
        ["unknown-worker"],
        ["worker-primary", "worker-helper"],
        ["worker-helper", "worker-helper"],
    ],
)
def test_start_rejects_invalid_helper_worker_ids(client, helper_worker_ids):
    store = create_store(client, "990299")
    create_product(client, store["id"], "1005009929901")
    register_browser_worker(client, "worker-primary", "主机")
    register_browser_worker(client, "worker-helper", "辅助机")
    assert select_browser_worker(client, "worker-primary").status_code == 200

    response = client.post(
        "/api/browser-collection/runs/start-today",
        json={"helper_worker_ids": helper_worker_ids},
    )
    today = client.get("/api/browser-collection/runs/today").json()

    assert response.status_code == 409
    assert today["dispatch_enabled"] is False
    assert today["primary_worker_id"] is None


def test_multiple_helpers_claim_distinct_items_and_unselected_worker_cannot_claim(
    client, db_session
):
    stores = [create_store(client, store_id) for store_id in ("990298", "990297", "990296", "990295")]
    for index, store in enumerate(stores, start=1):
        create_product(client, store["id"], f"10050099298{index}")
    workers = {
        worker_id: register_browser_worker(client, worker_id, label)
        for worker_id, label in (
            ("worker-primary", "主机"),
            ("worker-helper-a", "辅助机 A"),
            ("worker-helper-b", "辅助机 B"),
            ("worker-helper-offline", "离线辅助机"),
            ("worker-unselected", "未启用电脑"),
        )
    }
    assert all(response.status_code == 200 for response in workers.values())
    assert select_browser_worker(client, "worker-primary").status_code == 200

    offline_helper = db_session.get(BrowserCollectionWorker, "worker-helper-offline")
    offline_helper.last_heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=3)
    db_session.commit()

    started = client.post(
        "/api/browser-collection/runs/start-today",
        json={
            "helper_worker_ids": [
                "worker-helper-a",
                "worker-helper-b",
                "worker-helper-offline",
            ]
        },
    )
    assert started.status_code == 200, started.text
    assert started.json()["primary_worker_id"] == "worker-primary"
    assert started.json()["helper_worker_ids"] == [
        "worker-helper-a",
        "worker-helper-b",
        "worker-helper-offline",
    ]

    claims = {
        worker_id: client.post(
            "/api/browser-collection/items/claim", json={"worker_id": worker_id}
        ).json()
        for worker_id in ("worker-primary", "worker-helper-a", "worker-helper-b")
    }
    offline = client.post(
        "/api/browser-collection/items/claim",
        json={"worker_id": "worker-helper-offline"},
    ).json()
    unselected = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-unselected"}
    ).json()

    assert all(claim["item"] is not None for claim in claims.values())
    assert len({claim["item"]["id"] for claim in claims.values()}) == 3
    assert all(claim["claim_token"] for claim in claims.values())
    assert offline["item"] is None
    assert offline["claim_token"] is None
    assert unselected["item"] is None
    assert unselected["claim_token"] is None


def test_locked_run_roles_do_not_follow_global_primary_selection(client):
    store = create_store(client, "990294")
    create_product(client, store["id"], "1005009929401")
    register_browser_worker(client, "worker-primary", "主机")
    register_browser_worker(client, "worker-helper", "辅助机")
    register_browser_worker(client, "worker-next", "下一轮主机")
    assert select_browser_worker(client, "worker-primary").status_code == 200
    started = client.post(
        "/api/browser-collection/runs/start-today",
        json={"helper_worker_ids": ["worker-helper"]},
    )
    assert started.status_code == 200, started.text

    assert select_browser_worker(client, "worker-next").status_code == 200
    changed = client.post(
        "/api/browser-collection/runs/start-today",
        json={"helper_worker_ids": ["worker-next"]},
    )
    unselected = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-next"}
    ).json()
    current = client.get("/api/browser-collection/runs/today").json()

    assert changed.status_code == 409
    assert current["primary_worker_id"] == "worker-primary"
    assert current["helper_worker_ids"] == ["worker-helper"]
    assert unselected["item"] is None
    assert unselected["claim_token"] is None


def test_legacy_resume_assigns_roles_without_changing_existing_claim(client, db_session):
    now = datetime.now(timezone.utc)
    primary = BrowserCollectionWorker(
        worker_id="legacy-primary", label="QX", last_heartbeat_at=now
    )
    helper = BrowserCollectionWorker(
        worker_id="legacy-helper", label="Helper", last_heartbeat_at=now
    )
    original_owner = BrowserCollectionWorker(
        worker_id="legacy-owner", label="Old owner", last_heartbeat_at=now
    )
    run = BrowserCollectionRun(
        target_date=date.today(), status="PAUSED", phase="STORE_DISCOVERY",
        total_count=1, started_at=now,
    )
    item = BrowserCollectionItem(
        run=run,
        target_type="STORE",
        target_key="store:legacy",
        target_url="https://www.aliexpress.com/store/legacy/pages/all-items.html",
        position=1,
        status="RUNNING",
        claimed_at=now,
        lease_expires_at=now + timedelta(minutes=5),
        claimed_worker_id=original_owner.worker_id,
        claim_token="legacy-valid-token",
    )
    control = BrowserCollectionControl(
        id=1, selected_worker_id=primary.worker_id, selected_worker_label=primary.label
    )
    db_session.add_all([primary, helper, original_owner, run, item, control])
    db_session.commit()

    resumed = client.post(
        f"/api/browser-collection/runs/{run.id}/resume",
        json={"helper_worker_ids": [helper.worker_id]},
    )

    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["primary_worker_id"] == primary.worker_id
    assert resumed.json()["helper_worker_ids"] == [helper.worker_id]
    db_session.refresh(item)
    assert item.claimed_worker_id == original_owner.worker_id
    assert item.claim_token == "legacy-valid-token"

    accepted = client.post(
        f"/api/browser-collection/items/{item.id}/failure",
        json={
            "worker_id": original_owner.worker_id,
            "claim_token": "legacy-valid-token",
            "error_type": "test-result",
            "error_message": "existing owner may finish",
        },
    )
    assert accepted.status_code == 200, accepted.text


def test_resume_requires_an_authorized_worker_online(client, db_session):
    now = datetime.now(timezone.utc)
    primary = BrowserCollectionWorker(
        worker_id="offline-primary", label="Primary", last_heartbeat_at=now - timedelta(minutes=3)
    )
    helper = BrowserCollectionWorker(
        worker_id="offline-helper", label="Helper", last_heartbeat_at=now - timedelta(minutes=3)
    )
    unselected = BrowserCollectionWorker(
        worker_id="online-unselected", label="Unselected", last_heartbeat_at=now
    )
    run = BrowserCollectionRun(
        target_date=date.today(), status="PAUSED", phase="STORE_DISCOVERY", total_count=1,
        primary_worker_id=primary.worker_id, helper_workers=[helper],
    )
    item = BrowserCollectionItem(
        run=run, target_type="STORE", target_key="store:pending",
        target_url="https://www.aliexpress.com/store/pending/pages/all-items.html",
        position=1, status="PENDING",
    )
    control = BrowserCollectionControl(
        id=1, selected_worker_id=unselected.worker_id, selected_worker_label=unselected.label
    )
    db_session.add_all([primary, helper, unselected, run, item, control])
    db_session.commit()

    resumed = client.post(f"/api/browser-collection/runs/{run.id}/resume")

    assert resumed.status_code == 409
    db_session.refresh(run)
    assert run.dispatch_enabled is False


def test_expired_claim_can_only_be_reclaimed_by_run_member(client, db_session):
    store = create_store(client, "990293")
    create_product(client, store["id"], "1005009929301")
    for worker_id in ("worker-primary", "worker-helper", "worker-unselected"):
        register_browser_worker(client, worker_id, worker_id)
    assert select_browser_worker(client, "worker-primary").status_code == 200
    started = client.post(
        "/api/browser-collection/runs/start-today",
        json={"helper_worker_ids": ["worker-helper"]},
    )
    assert started.status_code == 200, started.text
    first = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-primary"}
    ).json()
    item = db_session.get(BrowserCollectionItem, first["item"]["id"])
    item.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.commit()

    unselected = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-unselected"}
    ).json()
    helper = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-helper"}
    ).json()

    assert unselected["item"] is None
    assert unselected["claim_token"] is None
    assert helper["item"]["id"] == item.id
    assert helper["claim_token"] != first["claim_token"]


def test_only_primary_and_explicit_helper_can_claim_shared_queue(client):
    first_store = create_store(client, "990203")
    create_product(client, first_store["id"], "1005009920301")
    second_store = create_store(client, "990202")
    create_product(client, second_store["id"], "1005009920201")
    third_store = create_store(client, "990204")
    create_product(client, third_store["id"], "1005009920401")
    register_browser_worker(client, "worker-galore", "Galore主机")
    register_browser_worker(client, "worker-second", "第二台电脑")
    register_browser_worker(client, "worker-unselected", "未启用电脑")
    assert select_browser_worker(client, "worker-galore").status_code == 200
    started = client.post(
        "/api/browser-collection/runs/start-today",
        json={"helper_worker_ids": ["worker-second"]},
    )
    assert started.status_code == 200, started.text

    helper = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-second"}
    )
    primary = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-galore"}
    )
    unselected = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-unselected"}
    )

    assert helper.status_code == 200
    assert primary.status_code == 200
    assert helper.json()["item"]["target_type"] == "STORE"
    assert primary.json()["item"]["target_type"] == "STORE"
    assert helper.json()["item"]["store_id"] != primary.json()["item"]["store_id"]
    assert unselected.json()["item"] is None
    assert unselected.json()["claim_token"] is None


def test_both_online_workers_claim_distinct_store_items(client):
    first_store = create_store(client, "990351")
    create_product(client, first_store["id"], "1005009935101")
    second_store = create_store(client, "990352")
    create_product(client, second_store["id"], "1005009935201")
    register_browser_worker(client, "worker-first", "采集电脑")
    register_browser_worker(client, "worker-second", "采集电脑")
    assert select_browser_worker(client, "worker-first").status_code == 200
    started = client.post(
        "/api/browser-collection/runs/start-today",
        json={"helper_worker_ids": ["worker-second"]},
    )
    assert started.status_code == 200, started.text

    legacy = client.post("/api/browser-collection/items/claim", json={}).json()
    unknown = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "unknown-worker"}
    ).json()
    first = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-first"}
    ).json()
    second = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-second"}
    ).json()

    assert legacy["item"] is None
    assert unknown["item"] is None
    assert first["item"] is not None
    assert second["item"] is not None
    assert first["item"]["id"] != second["item"]["id"]
    assert first["item"]["store_id"] != second["item"]["store_id"]
    assert first["claim_token"] != second["claim_token"]


def test_one_worker_can_claim_multiple_store_items(client):
    stores = [create_store(client, store_id) for store_id in ("990353", "990354")]
    for index, store in enumerate(stores, start=1):
        create_product(client, store["id"], f"10050099353{index}")
    register_browser_worker(client, "worker-first", "采集电脑")
    assert select_browser_worker(client, "worker-first").status_code == 200
    assert client.post("/api/browser-collection/runs/start-today").status_code == 200

    first = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-first"}
    ).json()
    second = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-first"}
    ).json()

    assert first["item"] is not None
    assert second["item"] is not None
    assert first["item"]["id"] != second["item"]["id"]


def test_single_online_worker_can_drain_entire_shared_queue(client, db_session):
    stores = [create_store(client, store_id) for store_id in ("990355", "990356", "990357")]
    for index, store in enumerate(stores, start=1):
        create_product(client, store["id"], f"10050099355{index}")
    register_browser_worker(client, "worker-only", "唯一在线电脑")
    assert select_browser_worker(client, "worker-only").status_code == 200
    assert client.post("/api/browser-collection/runs/start-today").status_code == 200

    from app.services.browser_collection import finish_item

    claimed_ids = []
    for _ in stores:
        response = client.post(
            "/api/browser-collection/items/claim", json={"worker_id": "worker-only"}
        ).json()
        assert response["item"] is not None
        claimed_ids.append(response["item"]["id"])
        finish_item(
            db_session,
            db_session.get(BrowserCollectionItem, response["item"]["id"]),
            "SUCCEEDED",
        )

    empty = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-only"}
    ).json()

    assert len(set(claimed_ids)) == len(stores)
    assert empty["item"] is None


def test_selected_worker_offline_after_start_does_not_block_other_online_claims(
    client, db_session
):
    stores = [create_store(client, store_id) for store_id in ("990358", "990359")]
    for index, store in enumerate(stores, start=1):
        create_product(client, store["id"], f"10050099358{index}")
    register_browser_worker(client, "worker-first", "第一台电脑")
    register_browser_worker(client, "worker-second", "第二台电脑")
    assert select_browser_worker(client, "worker-first").status_code == 200
    started = client.post(
        "/api/browser-collection/runs/start-today",
        json={"helper_worker_ids": ["worker-second"]},
    )
    assert started.status_code == 200, started.text
    worker = db_session.get(BrowserCollectionWorker, "worker-first")
    worker.last_heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=3)
    db_session.commit()

    claim = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-second"}
    ).json()

    assert claim["item"] is not None


def test_stale_or_missing_worker_cannot_claim_shared_queue(client, db_session):
    stores = [create_store(client, store_id) for store_id in ("990360", "990361")]
    for index, store in enumerate(stores, start=1):
        create_product(client, store["id"], f"10050099360{index}")
    register_browser_worker(client, "worker-first", "第一台电脑")
    register_browser_worker(client, "worker-stale", "离线电脑")
    assert select_browser_worker(client, "worker-first").status_code == 200
    assert client.post("/api/browser-collection/runs/start-today").status_code == 200
    stale_worker = db_session.get(BrowserCollectionWorker, "worker-stale")
    stale_worker.last_heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=3)
    db_session.commit()

    missing = client.post("/api/browser-collection/items/claim", json={}).json()
    stale = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-stale"}
    ).json()
    current = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-first"}
    ).json()

    assert missing["item"] is None
    assert stale["item"] is None
    assert current["item"] is not None


def test_expired_claim_can_be_reclaimed_by_another_online_worker(client, db_session):
    store = create_store(client, "990362")
    create_product(client, store["id"], "1005009936201")
    register_browser_worker(client, "worker-first", "第一台电脑")
    register_browser_worker(client, "worker-second", "第二台电脑")
    assert select_browser_worker(client, "worker-first").status_code == 200
    started = client.post(
        "/api/browser-collection/runs/start-today",
        json={"helper_worker_ids": ["worker-second"]},
    )
    assert started.status_code == 200, started.text
    first = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-first"}
    ).json()
    item = db_session.get(BrowserCollectionItem, first["item"]["id"])
    item.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.commit()

    second = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-second"}
    ).json()
    stale = client.post(
        f"/api/browser-collection/items/{item.id}/failure",
        json={
            "worker_id": "worker-first",
            "claim_token": first["claim_token"],
            "error_type": "test",
            "error_message": "expired attempt",
        },
    )

    assert second["item"]["id"] == item.id
    assert second["claim_token"] != first["claim_token"]
    assert stale.status_code == 409
    db_session.refresh(item)
    assert item.status == "RUNNING"
    assert item.claimed_worker_id == "worker-second"


def test_selecting_start_worker_does_not_expand_locked_run_membership(client):
    stores = [create_store(client, store_id) for store_id in ("990363", "990364")]
    for index, store in enumerate(stores, start=1):
        create_product(client, store["id"], f"10050099363{index}")
    register_browser_worker(client, "worker-first", "第一台电脑")
    register_browser_worker(client, "worker-second", "第二台电脑")
    assert select_browser_worker(client, "worker-first").status_code == 200
    assert client.post("/api/browser-collection/runs/start-today").status_code == 200
    first = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-first"}
    ).json()

    switched = select_browser_worker(client, "worker-second")
    unselected = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-second"}
    ).json()

    assert switched.status_code == 200
    assert switched.json()["dispatch_enabled"] is True
    assert unselected["item"] is None
    assert unselected["claim_token"] is None


def test_start_does_not_wait_for_other_worker_claims(client):
    stores = [create_store(client, store_id) for store_id in ("990365", "990366")]
    for index, store in enumerate(stores, start=1):
        create_product(client, store["id"], f"10050099365{index}")
    register_browser_worker(client, "worker-first", "第一台电脑")
    register_browser_worker(client, "worker-second", "第二台电脑")
    assert select_browser_worker(client, "worker-first").status_code == 200
    started = client.post("/api/browser-collection/runs/start-today").json()
    first = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-first"}
    ).json()
    paused = client.post(f"/api/browser-collection/runs/{started['id']}/pause")
    assert paused.status_code == 200
    assert select_browser_worker(
        client, "worker-second", confirm_handoff=True
    ).status_code == 200

    resumed = client.post("/api/browser-collection/runs/start-today")

    assert first["item"] is not None
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["dispatch_enabled"] is True


def test_parallel_store_items_keep_store_phase_until_all_terminal(client, db_session):
    stores = [create_store(client, store_id) for store_id in ("990367", "990368")]
    for index, store in enumerate(stores, start=1):
        create_product(client, store["id"], f"10050099367{index}")
    register_browser_worker(client, "worker-first", "第一台电脑")
    register_browser_worker(client, "worker-second", "第二台电脑")
    assert select_browser_worker(client, "worker-first").status_code == 200
    started = client.post(
        "/api/browser-collection/runs/start-today",
        json={"helper_worker_ids": ["worker-second"]},
    )
    assert started.status_code == 200, started.text
    first = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-first"}
    ).json()
    second = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-second"}
    ).json()

    from app.services.browser_collection import finish_item

    finish_item(
        db_session,
        db_session.get(BrowserCollectionItem, first["item"]["id"]),
        "SUCCEEDED",
    )
    run = db_session.get(BrowserCollectionRun, first["item"]["run_id"])
    db_session.refresh(run)

    assert second["item"] is not None
    assert run.phase == "STORE_DISCOVERY"
    assert run.status == "RUNNING"


@pytest.mark.parametrize("owner_online", [True, False])
def test_verification_resume_keeps_online_owner_or_requeues_offline_owner(
    client, db_session, owner_online
):
    store = create_store(client, "990369")
    create_product(client, store["id"], "1005009936901")
    register_browser_worker(client, "worker-first", "第一台电脑")
    register_browser_worker(client, "worker-second", "第二台电脑")
    assert select_browser_worker(client, "worker-first").status_code == 200
    run = client.post(
        "/api/browser-collection/runs/start-today",
        json={"helper_worker_ids": ["worker-second"]},
    ).json()
    claim = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-first"}
    ).json()
    challenged = client.post(
        f"/api/browser-collection/items/{claim['item']['id']}/challenge",
        json={
            "worker_id": "worker-first",
            "claim_token": claim["claim_token"],
            "error_message": "verification page",
            "page_url": "https://www.aliexpress.com/punish",
        },
    )
    assert challenged.status_code == 200
    if not owner_online:
        worker = db_session.get(BrowserCollectionWorker, "worker-first")
        worker.last_heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=3)
        db_session.commit()
    assert select_browser_worker(
        client, "worker-second", confirm_handoff=True
    ).status_code == 200

    resumed = client.post(f"/api/browser-collection/runs/{run['id']}/resume")

    assert resumed.status_code == 200, resumed.text
    item = db_session.get(BrowserCollectionItem, claim["item"]["id"])
    db_session.refresh(item)
    if owner_online:
        assert item.status == "RUNNING"
        assert item.claimed_worker_id == "worker-first"
        assert item.claim_token == claim["claim_token"]
    else:
        assert item.status == "PENDING"
        assert item.claimed_worker_id is None
        assert item.claim_token is None


def test_control_reports_active_claims_per_worker(client, db_session):
    stores = [create_store(client, store_id) for store_id in ("990370", "990371")]
    for index, store in enumerate(stores, start=1):
        create_product(client, store["id"], f"10050099370{index}")
    for worker_id in ("worker-first", "worker-second", "worker-idle"):
        register_browser_worker(client, worker_id, "采集电脑")
    assert select_browser_worker(client, "worker-first").status_code == 200
    started = client.post(
        "/api/browser-collection/runs/start-today",
        json={"helper_worker_ids": ["worker-second"]},
    )
    assert started.status_code == 200, started.text
    first = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-first"}
    ).json()
    second = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-second"}
    ).json()
    challenged = client.post(
        f"/api/browser-collection/items/{first['item']['id']}/challenge",
        json={
            "worker_id": "worker-first",
            "claim_token": first["claim_token"],
            "error_message": "verification page",
            "page_url": "https://www.aliexpress.com/punish",
        },
    )
    assert challenged.status_code == 200

    control = client.get("/api/browser-collection/control").json()
    workers = {worker["worker_id"]: worker for worker in control["workers"]}
    assert workers["worker-first"]["active_claim_count"] == 1
    assert workers["worker-second"]["active_claim_count"] == 1
    assert workers["worker-idle"]["active_claim_count"] == 0

    second_item = db_session.get(BrowserCollectionItem, second["item"]["id"])
    second_item.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.commit()
    control = client.get("/api/browser-collection/control").json()
    workers = {worker["worker_id"]: worker for worker in control["workers"]}
    assert workers["worker-second"]["active_claim_count"] == 0


def test_control_can_resume_locked_run_when_only_helper_is_online(client, db_session):
    now = datetime.now(timezone.utc)
    primary = BrowserCollectionWorker(
        worker_id="control-primary", label="Primary",
        last_heartbeat_at=now - timedelta(minutes=3),
    )
    helper = BrowserCollectionWorker(
        worker_id="control-helper", label="Helper", last_heartbeat_at=now,
    )
    run = BrowserCollectionRun(
        target_date=date.today(), status="PAUSED", phase="STORE_DISCOVERY", total_count=1,
        primary_worker_id=primary.worker_id, helper_workers=[helper],
    )
    item = BrowserCollectionItem(
        run=run, target_type="STORE", target_key="store:control",
        target_url="https://www.aliexpress.com/store/control/pages/all-items.html",
        position=1, status="PENDING",
    )
    control = BrowserCollectionControl(
        id=1, selected_worker_id=primary.worker_id, selected_worker_label=primary.label
    )
    db_session.add_all([primary, helper, run, item, control])
    db_session.commit()

    response = client.get("/api/browser-collection/control")

    assert response.status_code == 200
    assert response.json()["can_start"] is True


def test_control_can_start_while_another_worker_has_a_valid_claim(client):
    stores = [create_store(client, store_id) for store_id in ("990372", "990373")]
    for index, store in enumerate(stores, start=1):
        create_product(client, store["id"], f"10050099372{index}")
    register_browser_worker(client, "worker-first", "第一台电脑")
    register_browser_worker(client, "worker-second", "第二台电脑")
    assert select_browser_worker(client, "worker-first").status_code == 200
    assert client.post("/api/browser-collection/runs/start-today").status_code == 200
    first = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-first"}
    ).json()
    assert first["item"] is not None
    assert select_browser_worker(client, "worker-second").status_code == 200

    control = client.get("/api/browser-collection/control").json()
    started = client.post("/api/browser-collection/runs/start-today")

    assert control["can_start"] is True
    assert control["handoff_pending"] is False
    assert control["draining_count"] == 0
    assert started.status_code == 200, started.text


def test_control_disambiguates_duplicate_worker_labels(client, db_session):
    register_browser_worker(client, "worker-alpha-1234", "采集电脑")
    register_browser_worker(client, "worker-beta-5678", "采集电脑")
    assert select_browser_worker(client, "worker-alpha-1234").status_code == 200
    control_row = db_session.get(BrowserCollectionControl, 1)
    control_row.selected_worker_label = "Qx的computer"
    db_session.commit()

    control = client.get("/api/browser-collection/control").json()
    workers = {worker["worker_id"]: worker for worker in control["workers"]}

    assert control["selected_worker"]["label"] == "Qx的computer"
    assert workers["worker-alpha-1234"]["display_label"] == "Qx的computer"
    assert workers["worker-beta-5678"]["display_label"] == "采集电脑"
    control_row.selected_worker_label = "采集电脑"
    db_session.commit()
    workers = {
        worker["worker_id"]: worker
        for worker in client.get("/api/browser-collection/control").json()["workers"]
    }
    assert workers["worker-alpha-1234"]["display_label"] == "采集电脑 · worker-a"
    assert workers["worker-beta-5678"]["display_label"] == "采集电脑 · worker-b"
    assert workers["worker-alpha-1234"]["display_label"] != workers["worker-beta-5678"]["display_label"]
    assert db_session.get(BrowserCollectionWorker, "worker-alpha-1234").label == "采集电脑"
    assert db_session.get(BrowserCollectionWorker, "worker-beta-5678").label == "采集电脑"


def test_worker_display_name_override_survives_heartbeat_and_selection(client, db_session):
    qx_worker_id = "17413d70-7a33-4cde-96f1-3fb415ef0f8b"
    register_browser_worker(client, qx_worker_id, "第二台电脑")
    qx_worker = db_session.get(BrowserCollectionWorker, qx_worker_id)
    qx_worker.display_label_override = "Qx的computer"
    db_session.commit()

    register_browser_worker(client, "worker-galore", "Galore主机")
    assert select_browser_worker(client, "worker-galore").status_code == 200
    register_browser_worker(client, qx_worker_id, "第二台电脑")

    control = client.get("/api/browser-collection/control").json()
    workers = {worker["worker_id"]: worker for worker in control["workers"]}

    assert control["selected_worker"]["worker_id"] == "worker-galore"
    assert workers[qx_worker_id]["label"] == "Qx的computer"
    assert workers[qx_worker_id]["display_label"] == "Qx的computer"

    assert select_browser_worker(client, qx_worker_id).status_code == 200
    selected_control = client.get("/api/browser-collection/control").json()
    assert selected_control["selected_worker"]["label"] == "Qx的computer"


def test_non_selected_heartbeat_cannot_change_owner(client):
    register_browser_worker(client, "worker-galore", "Galore主机")
    register_browser_worker(client, "worker-second", "第二台电脑")
    assert select_browser_worker(client, "worker-galore").status_code == 200

    register_browser_worker(client, "worker-second", "第二台电脑")
    control = client.get("/api/browser-collection/control")

    assert control.status_code == 200
    assert control.json()["selected_worker"]["worker_id"] == "worker-galore"


def test_selected_worker_offline_does_not_block_shared_claims(client, db_session):
    store = create_store(client, "990201")
    create_product(client, store["id"], "1005009920101")
    register_browser_worker(client, "worker-galore", "Galore主机")
    assert select_browser_worker(client, "worker-galore").status_code == 200
    register_browser_worker(client, "worker-second", "第二台电脑")
    started = client.post(
        "/api/browser-collection/runs/start-today",
        json={"helper_worker_ids": ["worker-second"]},
    )
    assert started.status_code == 200, started.text
    selected = db_session.get(BrowserCollectionWorker, "worker-galore")
    selected.last_heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=3)
    db_session.commit()
    claim = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-second"}
    )
    control = client.get("/api/browser-collection/control")

    assert claim.status_code == 200
    assert claim.json()["item"] is not None
    assert control.json()["selected_worker"]["worker_id"] == "worker-galore"


def test_selecting_next_primary_does_not_expand_locked_run_membership(client, db_session):
    stores = [create_store(client, store_id) for store_id in ("990204", "990205")]
    for index, store in enumerate(stores):
        create_product(client, store["id"], f"10050099204{index}")
    register_browser_worker(client, "worker-galore", "Galore主机")
    register_browser_worker(client, "worker-second", "第二台电脑")
    assert select_browser_worker(client, "worker-galore").status_code == 200
    assert client.post("/api/browser-collection/runs/start-today").status_code == 200
    first = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-galore"}
    ).json()["item"]

    switched = select_browser_worker(client, "worker-second", confirm_handoff=True)
    started = client.post("/api/browser-collection/runs/start-today")
    unselected = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-second"}
    )
    assert switched.status_code == 200
    assert switched.json()["dispatch_enabled"] is True
    assert started.status_code == 200
    assert unselected.status_code == 200
    assert unselected.json()["item"] is None
    assert unselected.json()["claim_token"] is None



def test_concurrent_store_claim_is_allowed_while_current_store_runs(client, db_session):
    stores = [create_store(client, store_id) for store_id in ("990206", "990207")]
    for index, store in enumerate(stores):
        create_product(client, store["id"], f"10050099206{index}")
    register_browser_worker(client)
    assert select_browser_worker(client).status_code == 200
    assert client.post("/api/browser-collection/runs/start-today").status_code == 200

    first = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-galore"}
    ).json()["item"]
    second = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-galore"}
    ).json()["item"]

    assert first is not None
    assert second is not None
    assert first["id"] != second["id"]
    assert first["store_id"] != second["store_id"]
    first_item = db_session.get(BrowserCollectionItem, first["id"])
    from app.services.browser_collection import finish_item
    finish_item(db_session, first_item, "SUCCEEDED")
    current_run = db_session.get(BrowserCollectionRun, first_item.run_id)
    db_session.refresh(current_run)
    assert current_run.phase == "STORE_DISCOVERY"
    assert current_run.status == "RUNNING"
    next_claim = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-galore"}
    ).json()["item"]
    assert next_claim is None


def test_collect_all_and_retry_after_completion_leave_run_held(client, db_session):
    store = create_store(client, "990208")
    create_product(client, store["id"], "1005009920801")
    run = client.post("/api/browser-collection/runs/ensure", json={"trigger": "MANUAL"}).json()
    item = db_session.query(BrowserCollectionItem).one()
    item.status = "PARTIAL"
    item.completed_at = datetime.now(timezone.utc)
    db_session.commit()
    row = db_session.get(BrowserCollectionRun, run["id"])
    row.status = "PARTIAL"
    row.phase = "COMPLETED"
    row.completed_at = datetime.now(timezone.utc)
    db_session.commit()

    retried = client.post(f"/api/browser-collection/items/{item.id}/retry")
    assert retried.status_code == 200
    assert retried.json()["dispatch_enabled"] is False

    queued = client.post("/api/browser-collection/runs/collect-all")
    assert queued.status_code == 200
    assert queued.json()["run"]["dispatch_enabled"] is False


def test_selection_preserves_active_claim_and_rejects_stale_result(client, db_session):
    store = create_store(client, "990209")
    product = create_product(client, store["id"], "1005009920901")
    register_browser_worker(client, "worker-galore", "Galore主机")
    register_browser_worker(client, "worker-second", "第二台电脑")
    assert select_browser_worker(client, "worker-galore").status_code == 200
    started = client.post(
        "/api/browser-collection/runs/start-today",
        json={"helper_worker_ids": ["worker-second"]},
    )
    assert started.status_code == 200, started.text
    first_response = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-galore"}
    )
    first = first_response.json()
    first_token = first["claim_token"]
    item = db_session.get(BrowserCollectionItem, first["item"]["id"])

    assert "claim_token" not in client.get("/api/browser-collection/runs/today").json()
    assert "claim_token" not in first["item"]
    assert select_browser_worker(
        client, "worker-second", confirm_handoff=True
    ).status_code == 200
    item.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.commit()
    assert client.post("/api/browser-collection/runs/start-today").status_code == 200
    second = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-second"}
    ).json()

    stale_payload = monitored_store_payload(
        store["aliexpress_store_id"],
        product["aliexpress_product_id"],
        date.today(),
        {},
    )
    stale_payload.update(worker_id="worker-galore", claim_token=first_token)
    stale = client.post(
        f"/api/browser-collection/items/{item.id}/store-discovery", json=stale_payload
    )

    assert second["item"]["id"] == item.id
    assert second["claim_token"] != first_token
    assert stale.status_code == 409
    assert db_session.query(StoreSnapshot).count() == 0
    assert db_session.query(ProductDailyMetric).count() == 0


def test_wrong_worker_cannot_finish_claimed_item(client, db_session):
    store = create_store(client, "990210")
    create_product(client, store["id"], "1005009921001")
    register_browser_worker(client)
    assert select_browser_worker(client).status_code == 200
    assert client.post("/api/browser-collection/runs/start-today").status_code == 200
    claim = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-galore"}
    ).json()
    item = db_session.get(BrowserCollectionItem, claim["item"]["id"])

    response = client.post(
        f"/api/browser-collection/items/{item.id}/failure",
        json={
            "worker_id": "worker-second",
            "claim_token": claim["claim_token"],
            "error_type": "test",
            "error_message": "wrong worker",
        },
    )

    assert response.status_code == 409
    db_session.refresh(item)
    assert item.status == "RUNNING"


def test_store_identity_mismatch_is_not_automatically_retried(client, db_session):
    store = create_store(client, "990213")
    create_store(client, "9902131")
    create_product(client, store["id"], "1005009921301")
    claim = claim_item(client).json()
    item_id = claim["item"]["id"]

    response = client.post(
        f"/api/browser-collection/items/{item_id}/failure",
        json=add_claim_credentials({
            "error_type": "store_identity_mismatch",
            "error_message": "目标店铺 ID 与实际页面不一致",
        }, item_id),
    )

    assert response.status_code == 200
    assert db_session.get(BrowserCollectionItem, item_id).status == "FAILED"
    assert response.json()["failed_count"] == 1
    assert response.json()["status"] == "RUNNING"
    assert response.json()["paused_reason"] is None
    assert response.json()["dispatch_enabled"] is True
    assert response.json()["pending_count"] == 1
    assert request_claim(client).json()["item"]["store_id"] != store["id"]


def test_store_evidence_rejection_is_not_automatically_retried(client, db_session):
    store = create_store(client, "990215")
    create_product(client, store["id"], "1005009921501")
    claim = claim_item(client).json()["item"]

    response = client.post(
        f"/api/browser-collection/items/{claim['id']}/failure",
        json=add_claim_credentials({
            "error_type": "extension_collection_error",
            "error_message": "商品 ID 匹配与店铺页扫描证据不一致",
        }, claim["id"]),
    )

    assert response.status_code == 200
    assert db_session.get(BrowserCollectionItem, claim["id"]).status == "FAILED"
    assert response.json()["failed_count"] == 1
    assert response.json()["pending_count"] == 0


def test_product_collection_errors_keep_the_existing_retry_behavior(client, db_session):
    store = create_store(client, "990216")
    product = create_product(client, store["id"], "1005009921601")
    client.post(f"/api/products/{product['id']}/browser-collection")
    claim = claim_item(client).json()["item"]

    response = client.post(
        f"/api/browser-collection/items/{claim['id']}/failure",
        json=add_claim_credentials({
            "error_type": "extension_collection_error",
            "error_message": "商品页面临时读取失败",
        }, claim["id"]),
    )

    assert response.status_code == 200
    assert db_session.get(BrowserCollectionItem, claim["id"]).status == "PENDING"
    assert response.json()["pending_count"] == 1


def test_store_page_load_failure_is_retried_only_once(client, db_session):
    store = create_store(client, "990214")
    create_product(client, store["id"], "1005009921401")
    first_claim = claim_item(client).json()
    item_id = first_claim["item"]["id"]

    first_failure = client.post(
        f"/api/browser-collection/items/{item_id}/failure",
        json=add_claim_credentials({
            "error_type": "page_not_ready",
            "error_message": "店铺页暂时未加载完成",
        }, item_id),
    )

    assert first_failure.status_code == 200
    assert db_session.get(BrowserCollectionItem, item_id).status == "PENDING"
    second_claim = claim_item(client).json()
    assert second_claim["item"]["attempt_count"] == 2

    second_failure = client.post(
        f"/api/browser-collection/items/{item_id}/failure",
        json=add_claim_credentials({
            "error_type": "page_not_ready",
            "error_message": "店铺页暂时未加载完成",
        }, item_id),
    )

    assert second_failure.status_code == 200
    assert db_session.get(BrowserCollectionItem, item_id).status == "FAILED"


def test_collection_result_requires_claim_credentials(client, db_session):
    store = create_store(client, "990212")
    product = create_product(client, store["id"], "1005009921201")
    client.post(f"/api/products/{product['id']}/browser-collection")
    claim = claim_item(client).json()["item"]
    payload = product_payload(product["aliexpress_product_id"])
    payload.pop("worker_id", None)
    payload.pop("claim_token", None)

    response = client.post(
        f"/api/browser-collection/items/{claim['id']}/snapshot",
        json=payload,
    )

    assert response.status_code == 409
    assert db_session.query(ProductSnapshot).filter_by(product_id=product["id"]).count() == 0


def test_expired_claim_token_cannot_write_snapshot(client, db_session):
    store = create_store(client, "990211")
    product = create_product(client, store["id"], "1005009921101")
    run = BrowserCollectionRun(
        target_date=date.today(), trigger="MANUAL", status="PENDING",
        phase="PRODUCT_COLLECTION",
    )
    db_session.add(run)
    db_session.flush()
    item = BrowserCollectionItem(
        run_id=run.id,
        target_type="PRODUCT",
        target_key=f"product:{product['id']}:single",
        store_id=store["id"],
        product_id=product["id"],
        target_url=product["url"],
        position=1,
        status="PENDING",
    )
    db_session.add(item)
    db_session.commit()
    register_browser_worker(client)
    assert select_browser_worker(client).status_code == 200
    assert client.post("/api/browser-collection/runs/start-today").status_code == 200
    claim = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-galore"}
    ).json()
    item = db_session.get(BrowserCollectionItem, claim["item"]["id"])
    item.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.commit()
    payload = product_payload(product["aliexpress_product_id"])
    payload.update(worker_id="worker-galore", claim_token=claim["claim_token"])

    response = client.post(f"/api/browser-collection/items/{item.id}/snapshot", json=payload)

    assert response.status_code == 409
    assert db_session.query(ProductSnapshot).count() == 0


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
    captured_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    return add_claim_credentials({
        "platform_product_id": product_id,
        "url": f"https://www.aliexpress.com/item/{product_id}.html",
        "title": "Scheduled Product",
        "sold_count": sold_count,
        "price": "9.90",
        "rating": "4.7",
        "review_count": 44,
        "captured_at": captured_at,
        "raw_data": {"extractor_version": "scheduled-test"},
    })


def monitored_store_payload(store_id, product_id, target_date, values):
    payload = store_payload(store_id, count=0)
    payload["products"] = [{
        "platform_product_id": product_id,
        "url": f"https://www.aliexpress.com/item/{product_id}.html",
        "historical_sales": [
            {"date": metric_date.isoformat(), "value": value, "value_type": "daily_increment"}
            for metric_date, value in values.items()
        ],
        "history_source": "ixspy_visible_daily_increment",
    }]
    payload["raw_data"] = {
        "collection_mode": "monitored_store_daily",
        "monitored_product_ids": [product_id],
        "visited_page_urls": [payload["url"]],
        "history_target_date": target_date.isoformat(),
    }
    return add_claim_credentials(payload)


def test_queued_store_rejects_legacy_payload_without_pool_change(client, db_session):
    store = create_store(client, "990141")
    original = create_product(client, store["id"], "1005009914101")
    client.post("/api/browser-collection/runs/ensure", json={"trigger": "MANUAL"})
    claim = claim_item(client).json()["item"]

    response = client.post(
        f"/api/browser-collection/items/{claim['id']}/store-discovery",
        json=store_payload("990141", count=2),
    )

    assert response.status_code == 409
    assert {product.aliexpress_product_id for product in db_session.query(Product).all()} == {
        original["aliexpress_product_id"]
    }


def test_queued_store_writes_observed_and_pending_without_changing_pool(client, db_session):
    store = create_store(client, "990142")
    product = create_product(client, store["id"], "1005009914201")
    target_date = date.today()
    observed = {target_date - timedelta(days=1): 0, target_date - timedelta(days=2): 8}
    client.post("/api/browser-collection/runs/ensure", json={"trigger": "MANUAL"})
    claim = claim_item(client).json()["item"]

    response = client.post(
        f"/api/browser-collection/items/{claim['id']}/store-discovery",
        json=monitored_store_payload("990142", product["aliexpress_product_id"], target_date, observed),
    )

    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["status"] == "PARTIAL"
    assert db_session.query(Product).filter_by(store_id=store["id"]).count() == 1
    metrics = {metric.metric_date: metric for metric in db_session.query(ProductDailyMetric).all()}
    assert len(metrics) == 7
    assert metrics[target_date - timedelta(days=1)].estimated_sales == 0
    assert metrics[target_date - timedelta(days=1)].is_estimable is True
    assert metrics[target_date - timedelta(days=3)].estimated_sales == 0
    assert metrics[target_date - timedelta(days=3)].is_estimable is False
    assert metrics[target_date - timedelta(days=3)].reason == "ixspy_store_page_pending"


def test_queued_store_accepts_verified_route_alias_bound_to_task_store(client, db_session):
    store = create_store(client, "990142")
    product = create_product(client, store["id"], "1005009914201")
    target_date = date.today()
    payload = monitored_store_payload("990142", product["aliexpress_product_id"], target_date, {})
    alias_url = "https://www.aliexpress.com/store/990242/pages/all-items.html?shop_sortType=orders_desc"
    payload["url"] = alias_url
    payload["raw_data"]["visited_page_urls"] = [alias_url]
    payload["raw_data"]["store_identity"] = {
        "target_store_id": "990142",
        "route_store_id": "990242",
        "heading_store_id": "990142",
    }
    client.post("/api/browser-collection/runs/ensure", json={"trigger": "MANUAL"})
    claim = claim_item(client).json()["item"]

    response = client.post(
        f"/api/browser-collection/items/{claim['id']}/store-discovery",
        json=add_claim_credentials(payload),
    )

    assert response.status_code == 200, response.text
    assert db_session.query(Product).filter_by(store_id=store["id"]).count() == 1
    snapshot = db_session.query(StoreSnapshot).filter_by(store_id=store["id"]).one()
    assert snapshot.raw_payload["diagnostics"]["visited_page_urls"] == [alias_url]


def test_queued_store_accepts_route_alias_verified_by_explicit_store_id(client, db_session):
    store = create_store(client, "990152")
    product = create_product(client, store["id"], "1005009915201")
    target_date = date.today()
    alias_url = "https://www.aliexpress.com/store/990252/pages/all-items.html?shop_sortType=orders_desc"
    payload = monitored_store_payload("990152", product["aliexpress_product_id"], target_date, {})
    payload["url"] = alias_url
    payload["raw_data"]["visited_page_urls"] = [alias_url]
    payload["raw_data"]["store_identity"] = {
        "target_store_id": "990152",
        "route_store_id": "990252",
        "heading_store_id": None,
        "labeled_store_ids": ["990152"],
    }
    client.post("/api/browser-collection/runs/ensure", json={"trigger": "MANUAL"})
    claim = claim_item(client).json()["item"]

    response = client.post(
        f"/api/browser-collection/items/{claim['id']}/store-discovery",
        json=add_claim_credentials(payload),
    )

    assert response.status_code == 200, response.text
    snapshot = db_session.query(StoreSnapshot).filter_by(store_id=store["id"]).one()
    assert snapshot.raw_payload["diagnostics"]["visited_page_urls"] == [alias_url]


def test_queued_store_accepts_backend_approved_url_identity(client, db_session):
    store = create_store(client, "990154")
    product = create_product(client, store["id"], "1005009915401")
    target_date = date.today()
    alias_url = "https://www.aliexpress.com/store/990254/pages/all-items.html?shop_sortType=orders_desc"
    db_session.add(StoreUrlIdentity(
        store_id=store["id"],
        route_store_id="990254",
        normalized_page_identity="www.aliexpress.com/store/990254/pages/all-items.html",
        first_observed_url=alias_url,
        evidence={"ixspy_store_name": "Reviewed Store"},
    ))
    db_session.commit()
    payload = monitored_store_payload("990154", product["aliexpress_product_id"], target_date, {})
    payload["url"] = alias_url
    payload["raw_data"]["visited_page_urls"] = [alias_url]
    payload["raw_data"]["store_identity"] = {
        "target_store_id": "990154",
        "route_store_id": "990254",
        "heading_store_id": None,
        "labeled_store_ids": [],
        "ixspy_store_name": " Reviewed   Store ",
        "identity_method": "backend_approved_url_alias",
        "page_url": alias_url,
    }
    client.post("/api/browser-collection/runs/ensure", json={"trigger": "MANUAL"})
    claim = claim_item(client).json()["item"]

    response = client.post(
        f"/api/browser-collection/items/{claim['id']}/store-discovery",
        json=add_claim_credentials(payload),
    )

    assert response.status_code == 200, response.text


def test_queued_store_accepts_approved_orders_url_without_fresh_identity_proof(
    client, db_session
):
    store = create_store(client, "990156")
    product = create_product(client, store["id"], "1005009915601")
    alias_url = "https://www.aliexpress.com/store/990256/pages/all-items.html?shop_sortType=orders_desc"
    db_session.add(StoreUrlIdentity(
        store_id=store["id"],
        route_store_id="990256",
        normalized_page_identity="www.aliexpress.com/store/990256/pages/all-items.html",
        first_observed_url=alias_url,
        evidence={"page_title": "Reviewed Store", "identity_method": "unverified"},
    ))
    db_session.commit()
    payload = monitored_store_payload(
        "990156", product["aliexpress_product_id"], date.today(), {}
    )
    payload["url"] = alias_url
    payload["raw_data"]["visited_page_urls"] = [alias_url]
    client.post("/api/browser-collection/runs/ensure", json={"trigger": "MANUAL"})
    claim = claim_item(client).json()["item"]

    response = client.post(
        f"/api/browser-collection/items/{claim['id']}/store-discovery",
        json=add_claim_credentials(payload),
    )

    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["status"] == "PARTIAL"
    assert db_session.query(StoreSnapshot).filter_by(store_id=store["id"]).count() == 1


def test_queued_store_defers_approved_alias_with_conflicting_displayed_id(
    client, db_session
):
    store = create_store(client, "990157")
    product = create_product(client, store["id"], "1005009915701")
    alias_url = "https://www.aliexpress.com/store/990257/pages/all-items.html?shop_sortType=orders_desc"
    db_session.add(StoreUrlIdentity(
        store_id=store["id"],
        route_store_id="990257",
        normalized_page_identity="www.aliexpress.com/store/990257/pages/all-items.html",
        first_observed_url=alias_url,
        evidence={"ixspy_store_name": "Reviewed Store"},
    ))
    db_session.commit()
    payload = monitored_store_payload(
        "990157", product["aliexpress_product_id"], date.today(), {}
    )
    payload["url"] = alias_url
    payload["raw_data"]["visited_page_urls"] = [alias_url]
    payload["raw_data"]["store_identity"] = {
        "target_store_id": "990157",
        "route_store_id": "990257",
        "heading_store_id": "990999",
        "labeled_store_ids": ["990157"],
    }
    client.post("/api/browser-collection/runs/ensure", json={"trigger": "MANUAL"})
    claim = claim_item(client).json()["item"]

    response = client.post(
        f"/api/browser-collection/items/{claim['id']}/store-discovery",
        json=add_claim_credentials(payload),
    )

    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["status"] == "SKIPPED"
    assert db_session.query(StoreSnapshot).filter_by(store_id=store["id"]).count() == 0


def test_queued_store_rejects_backend_approved_url_identity_with_conflicting_name(
    client, db_session
):
    store = create_store(client, "990155")
    product = create_product(client, store["id"], "1005009915501")
    target_date = date.today()
    alias_url = "https://www.aliexpress.com/store/990255/pages/all-items.html?shop_sortType=orders_desc"
    db_session.add(StoreUrlIdentity(
        store_id=store["id"],
        route_store_id="990255",
        normalized_page_identity="www.aliexpress.com/store/990255/pages/all-items.html",
        first_observed_url=alias_url,
        evidence={"ixspy_store_name": "Reviewed Store"},
    ))
    db_session.commit()
    payload = monitored_store_payload("990155", product["aliexpress_product_id"], target_date, {})
    payload["url"] = alias_url
    payload["raw_data"]["visited_page_urls"] = [alias_url]
    payload["raw_data"]["store_identity"] = {
        "target_store_id": "990155",
        "route_store_id": "990255",
        "heading_store_id": None,
        "labeled_store_ids": [],
        "ixspy_store_name": "A Different Store",
        "identity_method": "backend_approved_url_alias",
        "page_url": alias_url,
    }
    client.post("/api/browser-collection/runs/ensure", json={"trigger": "MANUAL"})
    claim = claim_item(client).json()["item"]

    response = client.post(
        f"/api/browser-collection/items/{claim['id']}/store-discovery",
        json=add_claim_credentials(payload),
    )

    assert response.status_code == 200, response.text
    deferred = db_session.get(BrowserCollectionItem, claim["id"])
    assert deferred.status == "SKIPPED"
    assert deferred.error_type == "store_identity_deferred"
    assert deferred.identity_review_evidence["ixspy_store_name"] == "A Different Store"
    assert db_session.query(StoreSnapshot).count() == 0


def test_queued_store_defers_alias_with_conflicting_heading_even_if_store_id_label_matches(client, db_session):
    store = create_store(client, "990153")
    product = create_product(client, store["id"], "1005009915301")
    target_date = date.today()
    alias_url = "https://www.aliexpress.com/store/990253/pages/all-items.html?shop_sortType=orders_desc"
    payload = monitored_store_payload("990153", product["aliexpress_product_id"], target_date, {})
    payload["url"] = alias_url
    payload["raw_data"]["visited_page_urls"] = [alias_url]
    payload["raw_data"]["store_identity"] = {
        "target_store_id": "990153",
        "route_store_id": "990253",
        "heading_store_id": "990999",
        "labeled_store_ids": ["990153"],
    }
    client.post("/api/browser-collection/runs/ensure", json={"trigger": "MANUAL"})
    claim = claim_item(client).json()["item"]

    response = client.post(
        f"/api/browser-collection/items/{claim['id']}/store-discovery",
        json=add_claim_credentials(payload),
    )

    assert response.status_code == 200, response.text
    deferred = db_session.get(BrowserCollectionItem, claim["id"])
    assert deferred.status == "SKIPPED"
    assert deferred.error_type == "store_identity_deferred"
    assert deferred.identity_review_evidence["heading_store_id"] == "990999"
    assert db_session.query(StoreSnapshot).count() == 0


def test_queued_store_accepts_multiple_page_verified_route_aliases(client, db_session):
    store = create_store(client, "990242")
    product = create_product(client, store["id"], "1005009924201")
    target_date = date.today()
    payload = monitored_store_payload("990242", product["aliexpress_product_id"], target_date, {})
    alias_urls = [
        "https://www.aliexpress.com/store/990342/pages/all-items.html?page=1&shop_sortType=orders_desc",
        "https://www.aliexpress.com/store/990442/pages/all-items.html?page=2&shop_sortType=orders_desc",
    ]
    payload["raw_data"]["visited_page_urls"] = alias_urls
    payload["raw_data"]["store_identity"] = {
        "target_store_id": "990242",
        "route_store_id": "990442",
        "heading_store_id": "990242",
        "labeled_store_ids": ["990242"],
    }
    payload["raw_data"]["page_scans"] = [
        {
            "url": page_url,
            "store_identity": {
                "target_store_id": "990242",
                "route_store_id": page_url.split("/store/")[1].split("/")[0],
                "heading_store_id": "990242",
                "labeled_store_ids": ["990242"],
            },
        }
        for page_url in alias_urls
    ]
    client.post("/api/browser-collection/runs/ensure", json={"target_date": target_date.isoformat()})
    claim = claim_item(client).json()["item"]

    response = client.post(
        f"/api/browser-collection/items/{claim['id']}/store-discovery",
        json=add_claim_credentials(payload),
    )

    assert response.status_code == 200, response.text
    snapshot = db_session.query(StoreSnapshot).filter_by(store_id=store["id"]).one()
    assert snapshot.raw_payload["diagnostics"]["visited_page_urls"] == alias_urls


def test_queued_store_advances_candidate_cursor_on_verified_route_alias(client, db_session):
    store = create_store(client, "990149")
    route_alias_id = "990249"
    target_date = date.today()
    client.post("/api/browser-collection/runs/ensure", json={"target_date": target_date.isoformat()})
    claim = claim_item(client).json()["item"]
    alias_page_one = (
        f"https://www.aliexpress.com/store/{route_alias_id}/pages/all-items.html"
        "?sortType=bestmatch_sort&shop_sortType=orders_desc"
    )
    candidate = _history_candidate("1005009934901", target_date)
    candidate_page = _candidate_page(alias_page_one, route_alias_id, [candidate])
    payload = _daily_store_request("990149", alias_page_one, target_date, [], candidate_page)
    payload["raw_data"]["store_identity"] = {
        "target_store_id": "990149",
        "route_store_id": route_alias_id,
        "heading_store_id": "990149",
    }

    response = client.post(
        f"/api/browser-collection/items/{claim['id']}/store-discovery",
        json=add_claim_credentials(payload),
    )

    assert response.status_code == 200, response.text
    assert db_session.query(Product).filter_by(store_id=store["id"], status="active").count() == 1
    state = db_session.get(StorePageDiscoveryState, store["id"])
    assert state.last_outcome == "advanced"
    assert f"/store/{route_alias_id}/pages/all-items.html" in state.next_page_url
    assert "page=2" in state.next_page_url


def test_queued_store_rejects_route_alias_without_matching_page_identity(client, db_session):
    store = create_store(client, "990150")
    product = create_product(client, store["id"], "1005009915001")
    target_date = date.today()
    client.post("/api/browser-collection/runs/ensure", json={"target_date": target_date.isoformat()})
    claim = claim_item(client).json()["item"]
    alias_url = "https://www.aliexpress.com/store/990250/pages/all-items.html?shop_sortType=orders_desc"
    payload = monitored_store_payload(
        "990150", product["aliexpress_product_id"], target_date, {}
    )
    payload["url"] = alias_url
    payload["raw_data"]["visited_page_urls"] = [alias_url]
    payload["raw_data"]["store_identity"] = {
        "target_store_id": "990150",
        "route_store_id": "990250",
        "heading_store_id": "990150",
        "labeled_store_ids": ["990999"],
    }

    response = client.post(
        f"/api/browser-collection/items/{claim['id']}/store-discovery",
        json=add_claim_credentials(payload),
    )

    assert response.status_code == 200, response.text
    deferred = db_session.get(BrowserCollectionItem, claim["id"])
    assert deferred.status == "SKIPPED"
    assert deferred.error_type == "store_identity_deferred"
    assert deferred.identity_review_evidence["labeled_store_ids"] == ["990999"]
    assert db_session.query(StoreSnapshot).count() == 0


def test_queued_store_accepts_unique_title_matched_product_id_alias(client, db_session):
    store = create_store(client, "990143")
    monitored_id = "1005010587192858"
    observed_id = "3256810400878106"
    monitored = create_product(client, store["id"], monitored_id)
    target = db_session.get(Product, monitored["id"])
    target.title = "已采集 No-Drill Toothbrush Holder"
    db_session.commit()
    target_date = date.today()
    values = {target_date - timedelta(days=offset): offset for offset in range(1, 8)}
    client.post("/api/browser-collection/runs/ensure", json={"trigger": "MANUAL"})
    claim = claim_item(client).json()["item"]
    payload = monitored_store_payload("990143", monitored_id, target_date, values)
    page_url = payload["raw_data"]["visited_page_urls"][0]
    payload["products"][0].update({
        "title": "No-Drill Toothbrush Holder",
        "observed_product_id": observed_id,
        "observed_url": f"https://www.aliexpress.com/item/{observed_id}.html",
        "observed_page_url": page_url,
        "identity_match_method": "exact_title",
    })
    payload["raw_data"]["page_scans"] = [{
        "url": page_url,
        "visible_product_ids": [observed_id],
        "identity_matches": [{
            "monitored_product_id": monitored_id,
            "observed_product_id": observed_id,
            "match_method": "exact_title",
            "page_url": page_url,
        }],
    }]

    response = client.post(
        f"/api/browser-collection/items/{claim['id']}/store-discovery",
        json=add_claim_credentials(payload),
    )

    assert response.status_code == 200, response.text
    assert db_session.query(Product).filter_by(store_id=store["id"]).count() == 1
    assert db_session.get(Product, monitored["id"]).aliexpress_product_id == monitored_id
    snapshot = db_session.query(ProductSnapshot).filter_by(product_id=monitored["id"]).one()
    assert snapshot.raw_data["identity_match"] == {
        "monitored_product_id": monitored_id,
        "observed_product_id": observed_id,
        "observed_url": f"https://www.aliexpress.com/item/{observed_id}.html",
        "observed_page_url": page_url,
        "match_method": "exact_title",
    }


def test_queued_store_rejects_title_mismatch_for_observed_product_id_alias(client, db_session):
    store = create_store(client, "990144")
    monitored_id = "1005010587192858"
    observed_id = "3256810400878106"
    monitored = create_product(client, store["id"], monitored_id)
    db_session.get(Product, monitored["id"]).title = "No-Drill Toothbrush Holder"
    db_session.commit()
    target_date = date.today()
    payload = monitored_store_payload("990144", monitored_id, target_date, {})
    page_url = payload["raw_data"]["visited_page_urls"][0]
    payload["products"][0].update({
        "title": "A different product title",
        "observed_product_id": observed_id,
        "observed_url": f"https://www.aliexpress.com/item/{observed_id}.html",
        "observed_page_url": page_url,
        "identity_match_method": "exact_title",
    })
    payload["raw_data"]["page_scans"] = [{
        "url": page_url,
        "visible_product_ids": [observed_id],
        "identity_matches": [{
            "monitored_product_id": monitored_id,
            "observed_product_id": observed_id,
            "match_method": "exact_title",
        }],
    }]
    client.post("/api/browser-collection/runs/ensure", json={"trigger": "MANUAL"})
    claim = claim_item(client).json()["item"]

    response = client.post(
        f"/api/browser-collection/items/{claim['id']}/store-discovery",
        json=add_claim_credentials(payload),
    )

    assert response.status_code == 409
    assert "标题不匹配" in response.json()["detail"]
    assert db_session.query(ProductSnapshot).filter_by(product_id=monitored["id"]).count() == 0


def test_queued_store_persists_page_chart_and_worker_diagnostics(client, db_session):
    store = create_store(client, "990149")
    product = create_product(client, store["id"], "1005009914901")
    target_date = date.today()
    payload = monitored_store_payload("990149", product["aliexpress_product_id"], target_date, {})
    payload["raw_data"].update({
        "worker_id": "worker-second-pc",
        "worker_label": "第二台电脑",
        "page_scans": [{
            "url": payload["url"],
            "candidate_node_count": 3,
            "visible_product_count": 2,
            "matched_product_ids": [product["aliexpress_product_id"]],
            "unmatched_target_ids": [],
            "has_next_page": False,
            "next_page_url": None,
        }],
        "chart_reads": [{
            "product_id": product["aliexpress_product_id"],
            "outcome": "partial",
            "source": "ixspy_store_product_chart_unavailable",
            "point_count": 0,
        }],
    })
    client.post("/api/browser-collection/runs/ensure", json={"trigger": "MANUAL"})
    claim = claim_item(client).json()["item"]

    response = client.post(
        f"/api/browser-collection/items/{claim['id']}/store-discovery",
        json=add_claim_credentials(payload),
    )

    assert response.status_code == 200, response.text
    snapshot = db_session.query(StoreSnapshot).filter_by(store_id=store["id"]).one()
    diagnostics = snapshot.raw_payload["diagnostics"]
    assert diagnostics["worker_id"] == "worker-second-pc"
    assert diagnostics["worker_label"] == "第二台电脑"
    assert diagnostics["page_scans"][0]["candidate_node_count"] == 3
    assert diagnostics["chart_reads"][0]["source"] == "ixspy_store_product_chart_unavailable"


def test_queued_store_rejects_stale_targets_before_any_metric_write(client, db_session):
    store = create_store(client, "990143")
    product = create_product(client, store["id"], "1005009914301")
    client.post("/api/browser-collection/runs/ensure", json={"trigger": "MANUAL"})
    claim = claim_item(client).json()["item"]
    create_product(client, store["id"], "1005009914302")
    payload = monitored_store_payload("990143", product["aliexpress_product_id"], date.today(), {})

    response = client.post(f"/api/browser-collection/items/{claim['id']}/store-discovery", json=payload)

    assert response.status_code == 409
    assert db_session.query(ProductDailyMetric).count() == 0
    assert db_session.query(StoreSnapshot).count() == 0


def test_queued_store_rejects_unsorted_page_even_if_other_query_mentions_orders(client, db_session):
    store = create_store(client, "990147")
    product = create_product(client, store["id"], "1005009914701")
    client.post("/api/browser-collection/runs/ensure", json={"trigger": "MANUAL"})
    claim = claim_item(client).json()["item"]
    payload = monitored_store_payload("990147", product["aliexpress_product_id"], date.today(), {})
    unsorted = "https://www.aliexpress.com/store/990147/pages/all-items.html?shop_sortType=bestmatch_sort&spm=orders_desc"
    payload["raw_data"]["visited_page_urls"] = [unsorted]

    response = client.post(f"/api/browser-collection/items/{claim['id']}/store-discovery", json=payload)

    assert response.status_code == 409
    assert db_session.query(ProductDailyMetric).count() == 0


def test_queued_store_rejects_cumulative_like_series(client, db_session):
    store = create_store(client, "990144")
    product = create_product(client, store["id"], "1005009914401")
    client.post("/api/browser-collection/runs/ensure", json={"trigger": "MANUAL"})
    claim = claim_item(client).json()["item"]
    target_date = date.today()
    values = {
        target_date - timedelta(days=offset): 10000 + offset
        for offset in range(1, 8)
    }
    payload = monitored_store_payload("990144", product["aliexpress_product_id"], target_date, values)

    response = client.post(f"/api/browser-collection/items/{claim['id']}/store-discovery", json=payload)

    assert response.status_code == 409
    assert db_session.query(ProductDailyMetric).count() == 0


def test_queued_store_keeps_confirmed_value_when_later_page_misses_it(client, db_session):
    store = create_store(client, "990145")
    product = create_product(client, store["id"], "1005009914501")
    confirmed_date = date.today() - timedelta(days=1)
    db_session.add(ProductDailyMetric(
        product_id=product["id"], metric_date=confirmed_date,
        estimated_sales=9, is_estimable=True,
        reason="ixspy_visible_daily_increment",
        calculation_method="ixspy_visible_daily_increment",
    ))
    db_session.commit()
    client.post("/api/browser-collection/runs/ensure", json={"trigger": "MANUAL"})
    claim = claim_item(client).json()["item"]
    payload = monitored_store_payload("990145", product["aliexpress_product_id"], date.today(), {})

    response = client.post(f"/api/browser-collection/items/{claim['id']}/store-discovery", json=payload)

    assert response.status_code == 200
    metric = db_session.query(ProductDailyMetric).filter_by(product_id=product["id"], metric_date=confirmed_date).one()
    assert metric.estimated_sales == 9
    assert metric.is_estimable is True


def test_missing_store_chart_replaces_non_ixspy_estimate_with_pending_zero(client, db_session):
    store = create_store(client, "990146")
    product = create_product(client, store["id"], "1005009914601")
    metric_date = date.today() - timedelta(days=1)
    db_session.add(ProductDailyMetric(
        product_id=product["id"], metric_date=metric_date, estimated_sales=30,
        is_estimable=True, reason="estimated_from_public_cumulative_delta",
        calculation_method="public_cumulative_sold_delta",
    ))
    db_session.commit()
    client.post("/api/browser-collection/runs/ensure", json={"trigger": "MANUAL"})
    claim = claim_item(client).json()["item"]
    payload = monitored_store_payload("990146", product["aliexpress_product_id"], date.today(), {})

    response = client.post(f"/api/browser-collection/items/{claim['id']}/store-discovery", json=payload)

    assert response.status_code == 200
    metric = db_session.query(ProductDailyMetric).filter_by(product_id=product["id"], metric_date=metric_date).one()
    assert metric.estimated_sales == 0
    assert metric.is_estimable is False
    assert metric.reason == "ixspy_store_page_pending"


def test_retry_store_page_replaces_pending_zero_with_visible_increment(client, db_session):
    store = create_store(client, "990149")
    product = create_product(client, store["id"], "1005009914901")
    target_date = date.today()
    metric_date = target_date - timedelta(days=1)
    client.post("/api/browser-collection/runs/ensure", json={"trigger": "MANUAL"})
    first = claim_item(client).json()["item"]
    payload = monitored_store_payload("990149", product["aliexpress_product_id"], target_date, {})
    partial = client.post(f"/api/browser-collection/items/{first['id']}/store-discovery", json=payload)
    assert partial.status_code == 200
    assert partial.json()["items"][0]["status"] == "PARTIAL"
    assert partial.json()["status"] == "PARTIAL"
    assert partial.json()["dispatch_enabled"] is False

    retry = client.post(f"/api/browser-collection/items/{first['id']}/retry")
    assert retry.status_code == 200
    assert retry.json()["status"] == "RUNNING"
    assert retry.json()["dispatch_enabled"] is False
    resumed = client.post("/api/browser-collection/runs/start-today")
    assert resumed.status_code == 200
    assert resumed.json()["dispatch_enabled"] is True
    second = claim_item(client).json()["item"]
    complete_values = {
        target_date - timedelta(days=offset): offset for offset in range(1, 8)
    }
    payload = monitored_store_payload("990149", product["aliexpress_product_id"], target_date, complete_values)
    result = client.post(f"/api/browser-collection/items/{second['id']}/store-discovery", json=payload)

    assert result.status_code == 200, result.text
    assert result.json()["items"][0]["status"] == "SUCCEEDED"
    metric = db_session.query(ProductDailyMetric).filter_by(product_id=product["id"], metric_date=metric_date).one()
    assert metric.estimated_sales == 1
    assert metric.is_estimable is True


def test_ensure_daily_run_is_idempotent_and_starts_with_store_tasks(client, db_session):
    store = create_store(client)
    create_product(client, store["id"])
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


def test_scheduled_run_includes_active_store_without_products(client, db_session):
    empty = create_store(client, "990102")
    selected = create_store(client, "990103")
    create_product(client, selected["id"], "1005009910301")
    inactive = create_store(client, "990109")
    create_product(client, inactive["id"], "1005009910901")
    db_session.get(Store, inactive["id"]).status = "inactive"
    db_session.commit()

    response = client.post(
        "/api/browser-collection/runs/ensure",
        json={"target_date": "2026-09-21", "trigger": "MANUAL"},
    )

    assert response.status_code == 200
    items = db_session.query(BrowserCollectionItem).all()
    assert {item.store_id for item in items} == {empty["id"], selected["id"]}
    assert len(items) == 2
    assert all(item.target_type == "STORE" for item in items)


def test_scheduled_run_skips_custom_monitor_container_but_queues_real_stores(client, db_session):
    store = create_store(client, "990111")
    custom_product = client.post(
        "/api/products",
        json={"aliexpress_product_id": "1005009911101"},
    )
    assert custom_product.status_code == 201

    response = client.post(
        "/api/browser-collection/runs/ensure",
        json={"target_date": "2026-09-22", "trigger": "MANUAL"},
    )

    assert response.status_code == 200, response.text
    items = db_session.query(BrowserCollectionItem).all()
    assert [item.store_id for item in items] == [store["id"]]
    assert db_session.query(StorePageDiscoveryState).filter_by(
        store_id=custom_product.json()["store_id"]
    ).one_or_none() is None


def test_collect_all_queues_real_stores_when_custom_monitor_container_exists(client, db_session):
    store = create_store(client, "990112")
    custom_product = client.post(
        "/api/products",
        json={"aliexpress_product_id": "1005009911201"},
    )
    assert custom_product.status_code == 201

    response = client.post("/api/browser-collection/runs/collect-all")

    assert response.status_code == 200, response.text
    assert {item.store_id for item in db_session.query(BrowserCollectionItem).all()} == {store["id"]}


def test_store_claim_contains_only_active_monitored_ids(client, db_session):
    selected = create_store(client, "990106")
    other = create_store(client, "990107")
    tracked = create_product(client, selected["id"], "1005009910602")
    db_session.get(Product, tracked["id"]).title = "Tracked toothbrush holder"
    inactive = create_product(client, selected["id"], "1005009910601")
    create_product(client, other["id"], "1005009910701")
    db_session.get(Product, inactive["id"]).status = "inactive"
    db_session.commit()
    client.post("/api/browser-collection/runs/ensure", json={"trigger": "MANUAL"})

    claim = claim_item(client).json()["item"]

    assert claim["store_id"] == selected["id"]
    assert claim["monitored_product_ids"] == ["1005009910602"]
    assert claim["monitored_products"] == [{
        "platform_product_id": "1005009910602",
        "title": "Tracked toothbrush holder",
        "url": tracked["url"],
    }]


def test_manual_store_collection_queues_empty_pool_for_discovery(client, db_session):
    store = create_store(client, "990108")

    response = client.post(f"/api/stores/{store['id']}/browser-collection")

    assert response.status_code == 200, response.text
    assert response.json()["queued_count"] == 1
    item = claim_item(client).json()["item"]
    assert item["target_type"] == "STORE"
    assert item["monitored_product_ids"] == []
    assert item["discovery_page_url"].endswith("shop_sortType=orders_desc")
    assert db_session.query(BrowserCollectionItem).count() == 1


def test_store_claim_exposes_persisted_discovery_cursor(client, db_session):
    store = create_store(client, "990110")
    state = get_or_create_store_page_discovery_state(
        db_session, db_session.get(Store, store["id"])
    )
    next_url = (
        f"https://www.aliexpress.com/store/{store['aliexpress_store_id']}/pages/all-items.html"
        "?page=4&sortType=bestmatch_sort&shop_sortType=orders_desc"
    )
    advance_store_page_cursor(
        db_session,
        db_session.get(Store, store["id"]),
        scanned_url=state.next_page_url,
        has_next_page=True,
        next_page_url=next_url,
        scan_complete=True,
    )
    db_session.commit()
    client.post("/api/browser-collection/runs/ensure", json={"trigger": "MANUAL"})

    claim = claim_item(client).json()["item"]

    assert claim["store_id"] == store["id"]
    assert claim["monitored_product_ids"] == []
    assert claim["discovery_page_url"] == next_url
    persisted = db_session.get(StorePageDiscoveryState, store["id"])
    assert persisted.next_page_url == next_url


def test_manual_store_intake_rejects_empty_daily_mode_payload(client, db_session):
    store = create_store(client, "990148")
    product = create_product(client, store["id"], "1005009914801")
    payload = monitored_store_payload("990148", product["aliexpress_product_id"], date.today(), {})
    inactive = create_store(client, "990109")
    create_product(client, inactive["id"], "1005009910901")
    db_session.get(Store, inactive["id"]).status = "inactive"
    db_session.commit()
    payload["products"] = []

    response = client.post("/api/collection/browser-extension/store", json=payload)

    assert response.status_code == 409
    assert db_session.get(Product, product["id"]).status == "active"


def test_cancelled_run_requeues_only_selected_store(client, db_session):
    selected = create_store(client, "990104")
    other = create_store(client, "990105")
    create_product(client, selected["id"], "1005009910401")
    create_product(client, other["id"], "1005009910501")
    created = client.post("/api/browser-collection/runs/ensure", json={"trigger": "MANUAL"})
    cancel = client.post(f"/api/browser-collection/runs/{created.json()['id']}/cancel")
    assert cancel.status_code == 200

    response = client.post(f"/api/stores/{selected['id']}/browser-collection")

    assert response.status_code == 200
    items = db_session.query(BrowserCollectionItem).filter_by(target_type="STORE").all()
    assert {item.store_id: item.status for item in items} == {
        selected["id"]: "PENDING", other["id"]: "SKIPPED",
    }
    assert db_session.query(BrowserCollectionItem).filter_by(target_type="PRODUCT").count() == 0


def test_collect_all_requeues_failed_and_skipped_stores_but_preserves_success(
    client, db_session
):
    stores = [create_store(client, f"9910{index:02d}") for index in range(3)]
    for index, store in enumerate(stores):
        create_product(client, store["id"], f"1005009910{index:03d}")
    inactive_store = create_store(client, "991099")
    create_product(client, inactive_store["id"], "1005009910999")
    db_session.get(Store, inactive_store["id"]).status = "inactive"
    db_session.commit()

    run = client.post(
        "/api/browser-collection/runs/ensure", json={"trigger": "MANUAL"}
    ).json()
    client.post(f"/api/browser-collection/runs/{run['id']}/cancel")
    items = db_session.query(BrowserCollectionItem).filter_by(run_id=run["id"]).all()
    items[0].status = "SUCCEEDED"
    items[0].completed_at = datetime.now(timezone.utc)
    items[1].status = "FAILED"
    items[1].error_type = "browser_tab_closed"
    db_session.commit()

    response = client.post("/api/browser-collection/runs/collect-all")

    assert response.status_code == 200
    assert response.json()["queued_count"] == 2
    assert response.json()["run"]["status"] == "PENDING"
    assert response.json()["run"]["paused_reason"] is None
    assert {item.store_id: item.status for item in items} == {
        stores[0]["id"]: "SUCCEEDED",
        stores[1]["id"]: "PENDING",
        stores[2]["id"]: "PENDING",
    }
    assert db_session.query(BrowserCollectionItem).filter_by(target_type="PRODUCT").count() == 0

    repeated = client.post("/api/browser-collection/runs/collect-all")

    assert repeated.status_code == 200
    assert repeated.json()["queued_count"] == 0
    assert db_session.query(BrowserCollectionItem).filter_by(run_id=run["id"]).count() == 3


def test_collect_all_does_not_resume_a_manually_paused_queue(client, db_session):
    store = create_store(client, "991098")
    create_product(client, store["id"], "1005009910981")
    run = client.post(
        "/api/browser-collection/runs/ensure", json={"trigger": "MANUAL"}
    ).json()
    client.post(f"/api/browser-collection/runs/{run['id']}/pause")

    response = client.post("/api/browser-collection/runs/collect-all")

    assert response.status_code == 409
    assert db_session.query(BrowserCollectionRun).one().paused_reason == "manual_pause"
    assert db_session.query(BrowserCollectionItem).one().status == "PENDING"


def test_collect_all_adds_a_newly_monitored_store_without_duplicate_positions(
    client, db_session
):
    first = create_store(client, "991097")
    create_product(client, first["id"], "1005009910971")
    run = client.post(
        "/api/browser-collection/runs/ensure", json={"trigger": "MANUAL"}
    ).json()
    second = create_store(client, "991096")
    create_product(client, second["id"], "1005009910961")

    response = client.post("/api/browser-collection/runs/collect-all")

    assert response.status_code == 200
    assert response.json()["queued_count"] == 1
    items = db_session.query(BrowserCollectionItem).filter_by(run_id=run["id"]).all()
    assert {item.store_id for item in items} == {first["id"], second["id"]}
    assert sorted(item.position for item in items) == [1, 2]


def test_store_collection_button_queues_store_page_before_product_fallback(client, db_session):
    selected_store = create_store(client, "990120")
    other_store = create_store(client, "990121")
    create_product(client, selected_store["id"], "100500991201")
    create_product(client, other_store["id"], "100500991202")

    response = client.post(f"/api/stores/{selected_store['id']}/browser-collection")
    again = client.post(f"/api/stores/{selected_store['id']}/browser-collection")

    assert response.status_code == 200
    assert response.json()["queued_count"] == 1
    assert response.json()["discovery_queued"] is True
    assert again.json()["queued_count"] == 0
    assert again.json()["reused_count"] == 1
    tasks = db_session.query(BrowserCollectionItem).all()
    assert len(tasks) == 1
    assert tasks[0].target_type == "STORE"
    assert tasks[0].store_id == selected_store["id"]
    claimed = claim_item(client).json()["item"]
    assert claimed["target_type"] == "STORE"
    run = client.get("/api/browser-collection/runs/today").json()
    assert claimed["target_date"] == run["target_date"]


def test_single_product_collection_queues_only_the_selected_product(client, db_session):
    selected_store = create_store(client, "990129")
    other_store = create_store(client, "990130")
    selected = create_product(client, selected_store["id"], "100500991291")
    other = create_product(client, other_store["id"], "100500991301")

    response = client.post(f"/api/products/{selected['id']}/browser-collection")

    assert response.status_code == 200
    assert response.json()["queued"] is True
    assert response.json()["item"]["target_key"] == f"product:{selected['id']}:single"
    assert response.json()["item"]["product_id"] == selected["id"]
    assert response.json()["item"]["target_url"].endswith(
        "#monitor_product_id=100500991291"
    )
    assert db_session.query(BrowserCollectionItem).count() == 1
    assert db_session.query(BrowserCollectionItem).one().product_id == selected["id"]
    claim = claim_item(client).json()["item"]
    assert claim["product_id"] == selected["id"]
    assert claim["product_id"] != other["id"]


def test_single_product_collection_preserves_skipped_history_and_is_idempotent(
    client, db_session
):
    store = create_store(client, "990131")
    product = create_product(client, store["id"], "100500991311")
    run = BrowserCollectionRun(
        target_date=date.today(),
        trigger="MANUAL",
        status="PARTIAL",
        phase="COMPLETED",
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add(run)
    db_session.flush()
    skipped = BrowserCollectionItem(
        run_id=run.id,
        target_type="PRODUCT",
        target_key=f"product:{product['id']}",
        store_id=store["id"],
        product_id=product["id"],
        target_url=f"https://www.aliexpress.com/item/100500991311.html#monitor_product_id=100500991311",
        position=1,
        status="SKIPPED",
        error_type="queue_reduced_for_filter_test",
    )
    db_session.add(skipped)
    db_session.commit()

    response = client.post(f"/api/products/{product['id']}/browser-collection")
    repeated = client.post(f"/api/products/{product['id']}/browser-collection")

    assert response.status_code == 200
    assert response.json()["queued"] is True
    assert repeated.status_code == 200
    assert repeated.json()["queued"] is False
    assert skipped.status == "SKIPPED"
    assert db_session.query(BrowserCollectionItem).count() == 2
    targeted = db_session.query(BrowserCollectionItem).filter_by(
        target_key=f"product:{product['id']}:single"
    ).one()
    assert targeted.status == "PENDING"


def test_single_product_collection_does_not_unpause_a_paused_run(client, db_session):
    store = create_store(client, "990132")
    product = create_product(client, store["id"], "100500991321")
    run = BrowserCollectionRun(
        target_date=date.today(),
        trigger="MANUAL",
        status="PAUSED",
        phase="COMPLETED",
        paused_reason="manual_pause",
    )
    db_session.add(run)
    db_session.commit()

    response = client.post(f"/api/products/{product['id']}/browser-collection")

    assert response.status_code == 409
    assert db_session.query(BrowserCollectionItem).count() == 0
    assert db_session.query(BrowserCollectionRun).one().status == "PAUSED"


def test_store_collection_reopens_completed_run_for_store_page(client, db_session):
    store = create_store(client, "990126")
    create_product(client, store["id"], "100500991261")
    client.post("/api/browser-collection/runs/ensure", json={"trigger": "MANUAL"})
    run = db_session.query(BrowserCollectionRun).one()
    store_task = db_session.query(BrowserCollectionItem).filter_by(target_type="STORE").one()
    store_task.status = "FAILED"
    store_task.error_type = "store_identity_mismatch"
    run.phase = "COMPLETED"
    run.status = "PARTIAL"
    db_session.flush()

    response = client.post(f"/api/stores/{store['id']}/browser-collection")

    assert response.status_code == 200
    assert response.json()["run"]["phase"] == "STORE_DISCOVERY"
    claim = claim_item(client).json()["item"]
    assert claim["target_type"] == "STORE"
    assert claim["store_id"] == store["id"]


def test_retry_store_discovery_refreshes_url_after_store_id_is_corrected(client, db_session):
    store = create_store(client, "990127")
    create_product(client, store["id"], "100500991271")
    client.post(f"/api/stores/{store['id']}/browser-collection")
    item = db_session.query(BrowserCollectionItem).filter_by(target_type="STORE").one()
    item.status = "FAILED"
    item.error_type = "store_identity_mismatch"
    item.error_message = "旧店铺 ID 与当前页面不一致"
    db_session.get(Store, store["id"]).aliexpress_store_id = "990128"
    db_session.get(Store, store["id"]).url = "https://www.aliexpress.com/store/990128"
    db_session.flush()

    response = client.post(f"/api/browser-collection/items/{item.id}/retry")

    assert response.status_code == 200
    assert item.status == "PENDING"
    assert "/store/990128/pages/all-items.html" in item.target_url
    assert "shop_sortType=orders_desc" in item.target_url


@pytest.mark.skip(reason="retired: scheduled store collection requires an existing monitoring pool")
def test_store_collection_button_discovers_when_store_has_no_active_products(client, db_session):
    store = create_store(client, "990122")

    response = client.post(f"/api/stores/{store['id']}/browser-collection")

    assert response.status_code == 200
    assert response.json()["discovery_queued"] is True
    task = db_session.query(BrowserCollectionItem).one()
    assert task.target_type == "STORE"
    assert task.store_id == store["id"]
    claim = claim_item(client).json()["item"]
    assert claim["target_type"] == "STORE"
    assert claim["store_id"] == store["id"]

    result = client.post(
        f"/api/browser-collection/items/{claim['id']}/store-discovery",
        json=store_payload(store_id="990122", count=2),
    )

    assert result.status_code == 200
    product_tasks = db_session.query(BrowserCollectionItem).filter_by(target_type="PRODUCT").all()
    assert len(product_tasks) == 2
    assert all(task.status == "PENDING" for task in product_tasks)
    assert db_session.query(ProductSnapshot).filter_by(source="CHROME_EXTENSION").count() == 2


@pytest.mark.skip(reason="retired: queued store submissions now require exact store identity")
def test_store_task_resolves_store_from_bound_task_when_extension_id_differs(client, db_session):
    store = create_store(client, "990123")
    queued = client.post(f"/api/stores/{store['id']}/browser-collection").json()
    claim = claim_item(client).json()["item"]
    payload = store_payload(store_id="990123", count=3)
    payload["platform_store_id"] = "stale-extension-id"
    payload["url"] = "https://www.aliexpress.com/store/all-items.html?sortType=orders_desc"

    response = client.post(
        f"/api/browser-collection/items/{claim['id']}/store-discovery",
        json=payload,
    )

    assert claim["store_id"] == store["id"]
    assert response.status_code == 200
    assert response.json()["succeeded_count"] == 1, response.json()
    products = db_session.query(Product).filter_by(store_id=store["id"], status="active").all()
    assert len(products) == 3


def test_store_task_rejects_explicit_url_for_another_store(client, db_session):
    store = create_store(client, "990124")
    product = create_product(client, store["id"], "100500991241")
    create_store(client, "990125")
    client.post(f"/api/stores/{store['id']}/browser-collection")
    claim = claim_item(client).json()["item"]
    payload = monitored_store_payload("990124", product["aliexpress_product_id"], date.today(), {})
    payload["url"] = "https://www.aliexpress.com/store/990125/pages/all-items.html?shop_sortType=orders_desc"

    response = client.post(
        f"/api/browser-collection/items/{claim['id']}/store-discovery",
        json=payload,
    )

    assert response.status_code == 200, response.text
    deferred = db_session.get(BrowserCollectionItem, claim["id"])
    assert deferred.status == "SKIPPED"
    assert deferred.error_type == "store_identity_deferred"
    assert deferred.identity_review_evidence["route_store_id"] == "990125"
    assert db_session.query(Product).filter_by(store_id=store["id"]).count() == 1
    assert db_session.query(StoreSnapshot).filter_by(store_id=store["id"]).count() == 0


@pytest.mark.skip(reason="retired: queued store path no longer uses public cumulative sales or detail fallback")
def test_store_sales_do_not_replace_ixspy_product_history_collection(client, db_session):
    store = create_store(client, "990101")
    today = date.today()
    product_id = "1005009910000"
    product = create_product(client, store["id"], product_id)
    previous_day = datetime.combine(
        today - timedelta(days=1), datetime.min.time(), tzinfo=ZoneInfo("Asia/Shanghai")
    ) + timedelta(hours=12)
    db_session.add(
        ProductSnapshot(
            product_id=product["id"],
            collected_at=previous_day,
            captured_at=previous_day,
            collector_name="baseline_fixture",
            collector_version="previous-store-snapshot",
            parse_status="success",
            cumulative_sold=990,
            sold_count=990,
            source="CHROME_EXTENSION",
            status="VALID",
            raw_data={"collection_mode": "store_page"},
        )
    )
    db_session.commit()
    client.post(
        "/api/browser-collection/runs/ensure",
        json={"target_date": today.isoformat(), "trigger": "MANUAL"},
    )
    claim = claim_item(client)
    assert claim.json()["item"]["target_type"] == "STORE"

    result = client.post(
        f"/api/browser-collection/items/{claim.json()['item']['id']}/store-discovery",
        json=store_payload(store_id="990101"),
    )
    assert result.status_code == 200
    assert result.json()["phase"] == "PRODUCT_COLLECTION"
    products = db_session.query(Product).filter_by(store_id=store["id"], status="active").all()
    assert len(products) == 20
    product_items = db_session.query(BrowserCollectionItem).filter_by(target_type="PRODUCT").all()
    assert len(product_items) == 20
    assert all(item.status == "PENDING" for item in product_items)
    snapshots = db_session.query(ProductSnapshot).filter_by(
        source="CHROME_EXTENSION", collector_name="chrome_extension_store"
    ).all()
    assert len(snapshots) == 20
    assert all(snapshot.status == "SUSPECT" for snapshot in snapshots)
    assert all(snapshot.sold_count is None for snapshot in snapshots)
    assert db_session.query(ProductDailyMetric).filter_by(product_id=product["id"]).count() == 0


@pytest.mark.skip(reason="retired: missing history stays pending for the next store-page run")
def test_store_discovery_queues_product_fallback_when_cumulative_sales_are_missing(client, db_session):
    store = create_store(client, "990109")
    client.post(
        "/api/browser-collection/runs/ensure",
        json={"target_date": date.today().isoformat(), "trigger": "MANUAL"},
    )
    store_claim = claim_item(client).json()["item"]
    discovery = store_payload(store_id="990109", count=1)
    discovery["products"][0]["public_cumulative_sold"] = None

    response = client.post(
        f"/api/browser-collection/items/{store_claim['id']}/store-discovery",
        json=discovery,
    )

    assert response.status_code == 200
    fallback = db_session.query(BrowserCollectionItem).filter_by(target_type="PRODUCT").one()
    assert fallback.product_id is not None
    assert fallback.status == "PENDING"


def test_store_discovery_imports_complete_signed_ixspy_history_without_product_page_fallback(client, db_session):
    store = create_store(client, "990134")
    tracked = create_product(client, store["id"], "1005009910000")
    target_date = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    expected_dates = [target_date - timedelta(days=offset) for offset in range(7, 0, -1)]
    client.post(
        "/api/browser-collection/runs/ensure",
        json={"target_date": target_date.isoformat(), "trigger": "MANUAL"},
    )
    claim = claim_item(client).json()["item"]
    discovery = monitored_store_payload(
        "990134", tracked["aliexpress_product_id"], target_date,
        dict(zip(expected_dates, [5, 0, -1, 2, 7, 1, 3], strict=True)),
    )

    response = client.post(
        f"/api/browser-collection/items/{claim['id']}/store-discovery",
        json=discovery,
    )

    assert response.status_code == 200, response.text
    product = db_session.query(Product).filter_by(store_id=store["id"]).one()
    metrics = db_session.query(ProductDailyMetric).filter_by(product_id=product.id).all()
    assert {metric.metric_date: metric.estimated_sales for metric in metrics} == dict(
        zip(expected_dates, [5, 0, -1, 2, 7, 1, 3], strict=True)
    )
    assert all(metric.is_estimable for metric in metrics)
    assert db_session.query(BrowserCollectionItem).filter_by(target_type="PRODUCT").count() == 0
    history_snapshot = db_session.query(ProductSnapshot).filter_by(product_id=product.id).one()
    assert history_snapshot.parse_status == "success"
    assert history_snapshot.raw_data["collection_mode"] == "monitored_store_daily"
    assert history_snapshot.raw_data["historical_sales"][2]["value"] == -1
    store_task = db_session.get(BrowserCollectionItem, claim["id"])
    assert store_task.snapshot_id is None
    assert response.json()["phase"] == "COMPLETED"


@pytest.mark.skip(reason="retired: automatic product-detail fallback is no longer queued")
def test_store_history_skips_preexisting_pending_product_but_retains_missing_fallback(client, db_session):
    store = create_store(client, "990135")
    complete = create_product(client, store["id"], "1005009910000")
    missing = create_product(client, store["id"], "1005009910001")
    run = BrowserCollectionRun(target_date=date.today(), trigger="MANUAL")
    db_session.add(run)
    db_session.flush()
    for position, product in enumerate((complete, missing), start=1):
        db_session.add(BrowserCollectionItem(
            run=run,
            target_type="PRODUCT",
            target_key=f"product:{product['id']}",
            store_id=store["id"],
            product_id=product["id"],
            target_url=f"https://www.aliexpress.com/item/{product['aliexpress_product_id']}.html",
            position=position,
            status="PENDING" if product == complete else "FAILED",
        ))
    db_session.commit()

    queued = client.post(f"/api/stores/{store['id']}/browser-collection")
    assert queued.status_code == 200
    assert queued.json()["queued_count"] == 1
    claim = claim_item(client).json()["item"]
    assert claim["target_type"] == "STORE"
    payload = store_payload(store_id="990135", count=2)
    expected_dates = [date.today() - timedelta(days=offset) for offset in range(7, 0, -1)]
    payload["products"][0].update({
        "historical_sales": [
            {"date": metric_date.isoformat(), "value": value, "value_type": "daily_increment"}
            for metric_date, value in zip(expected_dates, [5, 0, -1, 2, 7, 1, 3], strict=True)
        ],
        "history_source": "ixspy_visible_daily_increment",
        "history_daily_capture_complete": True,
        "history_expected_dates": [metric_date.isoformat() for metric_date in expected_dates],
        "history_missing_dates": [],
    })
    result = client.post(
        f"/api/browser-collection/items/{claim['id']}/store-discovery", json=payload,
    )

    assert result.status_code == 200, result.text
    items = db_session.query(BrowserCollectionItem).filter_by(target_type="PRODUCT").all()
    by_product = {item.product_id: item for item in items}
    assert by_product[complete["id"]].status == "SKIPPED"
    assert by_product[complete["id"]].error_type == "store_history_complete"
    assert by_product[missing["id"]].status == "PENDING"
    next_item = claim_item(client).json()["item"]
    assert next_item["product_id"] == missing["id"]


@pytest.mark.skip(reason="retired: store-only queue does not create product-detail fallback")
def test_store_discovery_skips_product_with_recent_history_already_captured_today(client, db_session):
    store = create_store(client, "990108")
    product = create_product(client, store["id"], "100500998001")
    today = date.today()
    client.post(
        "/api/browser-collection/runs/ensure",
        json={"target_date": today.isoformat(), "trigger": "MANUAL"},
    )
    now = datetime.now(timezone.utc)
    snapshot = ProductSnapshot(
        product_id=product["id"],
        collected_at=now,
        captured_at=now,
        collector_name="chrome_extension",
        collector_version="test",
        parse_status="success",
        source="CHROME_EXTENSION",
        status="VALID",
        raw_data={
            "historical_sales": [
                {
                    "date": (today - timedelta(days=offset)).isoformat(),
                    "value": offset,
                    "value_type": "daily_increment",
                }
                for offset in range(7, 0, -1)
            ]
        },
    )
    db_session.add(snapshot)
    db_session.flush()
    for offset in range(7, 0, -1):
        db_session.add(
            ProductDailyMetric(
                product_id=product["id"],
                metric_date=today - timedelta(days=offset),
                estimated_sales=offset,
                is_estimable=True,
                reason="ixspy_visible_daily_increment",
                end_snapshot_id=snapshot.id,
                calculation_method="ixspy_visible_daily_increment",
            )
        )
    db_session.commit()
    store_claim = claim_item(client).json()["item"]
    discovery = store_payload(store_id="990108", count=1)
    discovery["products"][0].update(
        {
            "platform_product_id": "100500998001",
            "url": "https://www.aliexpress.com/item/100500998001.html",
        }
    )

    result = client.post(
        f"/api/browser-collection/items/{store_claim['id']}/store-discovery",
        json=discovery,
    )

    assert result.status_code == 200
    assert result.json()["phase"] == "COMPLETED"
    assert db_session.query(BrowserCollectionItem).filter_by(target_type="PRODUCT").count() == 0


def test_history_coverage_does_not_skip_product_for_daily_values_above_lifetime_sold(client, db_session):
    store = create_store(client, "990130")
    product = create_product(client, store["id"], "100500991301")
    today = date.today()
    now = datetime.now(timezone.utc)
    snapshot = ProductSnapshot(
        product_id=product["id"],
        collected_at=now,
        captured_at=now,
        collector_name="chrome_extension",
        collector_version="old-extension",
        parse_status="success",
        source="CHROME_EXTENSION",
        status="VALID",
        sold_count=3,
        raw_data={
            "historical_sales": [
                {
                    "date": (today - timedelta(days=offset)).isoformat(),
                    "value": 100 + offset,
                    "value_type": "daily_increment",
                }
                for offset in range(7, 0, -1)
            ]
        },
    )
    db_session.add(snapshot)
    db_session.flush()
    for offset in range(7, 0, -1):
        db_session.add(
            ProductDailyMetric(
                product_id=product["id"],
                metric_date=today - timedelta(days=offset),
                estimated_sales=None,
                is_estimable=False,
                reason="ixspy_daily_increment_exceeds_cumulative_sold",
                end_snapshot_id=snapshot.id,
                calculation_method="ixspy_visible_daily_increment",
            )
        )
    db_session.commit()

    assert product["id"] not in _products_with_recent_history_for_date(db_session, today)


def test_public_page_estimates_do_not_suppress_ixspy_history_collection(client, db_session):
    store = create_store(client, "990131")
    product = create_product(client, store["id"], "100500991302")
    today = date.today()
    for offset in range(7, 0, -1):
        db_session.add(
            ProductDailyMetric(
                product_id=product["id"],
                metric_date=today - timedelta(days=offset),
                estimated_sales=offset,
                is_estimable=True,
                reason="estimated_from_public_cumulative_delta",
                calculation_method="public_cumulative_sold_delta",
            )
        )
    db_session.commit()

    assert product["id"] not in _products_with_recent_history_for_date(db_session, today)

    for metric in db_session.query(ProductDailyMetric).filter_by(product_id=product["id"]):
        metric.calculation_method = "ixspy_visible_daily_increment"
        metric.reason = "ixspy_visible_daily_increment"
    db_session.commit()

    assert product["id"] in _products_with_recent_history_for_date(db_session, today)


def test_product_task_snapshot_is_atomic_and_keeps_raw_data(client, db_session):
    store = create_store(client, "990102")
    product = create_product(client, store["id"], "100500992001")
    client.post(f"/api/products/{product['id']}/browser-collection")
    claim = claim_item(client).json()["item"]
    assert claim["product_id"] == product["id"]

    snapshot_payload = product_payload("100500992001", 121)
    snapshot_payload["historical_sales"] = [
        {
            "date": (date.today() - timedelta(days=offset)).isoformat(),
            "value": offset,
            "value_type": "daily_increment",
        }
        for offset in range(7, 0, -1)
    ]
    response = client.post(
        f"/api/browser-collection/items/{claim['id']}/snapshot",
        json=snapshot_payload,
    )
    assert response.status_code == 200
    assert response.json()["succeeded_count"] == 1
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
    assert snapshot.sold_count is None
    assert snapshot.cumulative_sold is None
    assert db_session.get(BrowserCollectionItem, claim["id"]).status == "SUCCEEDED"


def test_public_cumulative_deltas_do_not_count_as_ixspy_seven_day_history(client, db_session):
    store = create_store(client, "990131")
    product = create_product(client, store["id"], "100500991311")
    target_date = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    for offset in range(8, 0, -1):
        captured_at = datetime(
            (target_date - timedelta(days=offset)).year,
            (target_date - timedelta(days=offset)).month,
            (target_date - timedelta(days=offset)).day,
            2,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        )
        db_session.add(
            ProductSnapshot(
                product_id=product["id"],
                collected_at=captured_at,
                captured_at=captured_at,
                collector_name="chrome_extension",
                collector_version="previous-day-snapshot",
                parse_status="success",
                source="CHROME_EXTENSION",
                status="VALID",
                cumulative_sold=100 + (8 - offset) * 10,
                raw_data={},
            )
        )
    db_session.flush()
    calculate_product_daily_metrics(db_session, product["id"], commit=False)
    assert _products_with_recent_history_for_date(db_session, target_date) == set()
    assert _products_with_recent_history_for_date(db_session, target_date) == set()
    run = BrowserCollectionRun(
        target_date=target_date,
        trigger="MANUAL",
        status="RUNNING",
        phase="PRODUCT_COLLECTION",
    )
    db_session.add(run)
    db_session.flush()
    task = BrowserCollectionItem(
        run_id=run.id,
        target_type="PRODUCT",
        target_key=f"product:{product['id']}:single",
        store_id=store["id"],
        product_id=product["id"],
        target_url="https://www.aliexpress.com/item/100500991311.html",
        position=1,
        status="PENDING",
        attempt_count=1,
    )
    db_session.add(task)
    db_session.commit()
    claimed = claim_item(client).json()["item"]

    response = client.post(
        f"/api/browser-collection/items/{claimed['id']}/snapshot",
        json=product_payload("100500991311", 180),
    )

    assert response.status_code == 200
    item_result = response.json()["items"][0]
    assert item_result["status"] == "PARTIAL", item_result
    assert item_result["error_type"] == "missing_ixspy_daily_increment"
    assert "0/7 天" in item_result["error_message"]
    assert db_session.query(ProductDailyMetric).filter_by(product_id=product["id"]).count() == 0


def test_complete_ixspy_seven_day_history_completes_product_task(client, db_session):
    store = create_store(client, "990132")
    product = create_product(client, store["id"], "100500991321")
    target_date = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    client.post(f"/api/products/{product['id']}/browser-collection")
    claim = claim_item(client).json()["item"]
    payload = product_payload("100500991321")
    payload["historical_sales"] = [
        {
            "date": (target_date - timedelta(days=offset)).isoformat(),
            "value": offset - 1,
            "value_type": "daily_increment",
        }
        for offset in range(7, 0, -1)
    ]

    response = client.post(
        f"/api/browser-collection/items/{claim['id']}/snapshot",
        json=payload,
    )

    assert response.status_code == 200
    assert db_session.get(BrowserCollectionItem, claim["id"]).status == "SUCCEEDED"
    metrics = db_session.query(ProductDailyMetric).filter_by(
        product_id=claim["product_id"], calculation_method="ixspy_visible_daily_increment"
    ).all()
    assert len(metrics) == 7
    assert 0 in {metric.estimated_sales for metric in metrics}


def test_product_snapshot_without_recent_history_is_partial(client, db_session):
    store = create_store(client, "990107")
    product = create_product(client, store["id"], "100500997001")
    client.post(f"/api/products/{product['id']}/browser-collection")
    claim = claim_item(client).json()["item"]

    incomplete_payload = product_payload("100500997001")
    incomplete_payload["historical_sales"] = [
        {
            "date": (date.today() - timedelta(days=offset)).isoformat(),
            "value": offset,
            "value_type": "daily_increment",
        }
        for offset in range(7, 1, -1)
    ]
    response = client.post(
        f"/api/browser-collection/items/{claim['id']}/snapshot",
        json=incomplete_payload,
    )

    assert response.status_code == 200
    assert response.json()["partial_count"] == 1
    item = db_session.get(BrowserCollectionItem, claim["id"])
    assert item.status == "PARTIAL"
    assert item.error_type == "missing_ixspy_daily_increment"
    assert "6/7 天" in item.error_message
    assert db_session.query(ProductSnapshot).filter_by(product_id=product["id"]).count() >= 1


@pytest.mark.skip(reason="retired: manual rerun no longer requeues automatic product-detail tasks")
def test_manual_today_rerun_requeues_only_products_missing_recent_history(client, db_session):
    store = create_store(client, "990109")
    tracked = create_product(client, store["id"], "100500999001")
    today = date.today()
    now = datetime.now(timezone.utc)
    snapshot = ProductSnapshot(
        product_id=tracked["id"],
        collected_at=now,
        captured_at=now,
        collector_name="chrome_extension",
        collector_version="test",
        parse_status="success",
        source="CHROME_EXTENSION",
        status="VALID",
        raw_data={
            "historical_sales": [
                {
                    "date": (today - timedelta(days=offset)).isoformat(),
                    "value": offset,
                    "value_type": "daily_increment",
                }
                for offset in range(7, 0, -1)
            ]
        },
    )
    db_session.add(snapshot)
    db_session.flush()
    for offset in range(7, 0, -1):
        db_session.add(
            ProductDailyMetric(
                product_id=tracked["id"],
                metric_date=today - timedelta(days=offset),
                estimated_sales=offset,
                is_estimable=True,
                reason="ixspy_visible_daily_increment",
                end_snapshot_id=snapshot.id,
                calculation_method="ixspy_visible_daily_increment",
            )
        )
    db_session.commit()
    client.post(
        "/api/browser-collection/runs/ensure",
        json={"target_date": today.isoformat(), "trigger": "MANUAL"},
    )
    store_claim = claim_item(client).json()["item"]
    discovery = store_payload(store_id="990109", count=2)
    discovery["products"][0].update(
        {"platform_product_id": "100500999001", "url": "https://www.aliexpress.com/item/100500999001.html"}
    )
    discovery["products"][1].update(
        {"platform_product_id": "100500999002", "url": "https://www.aliexpress.com/item/100500999002.html"}
    )
    discovery["products"][1]["public_cumulative_sold"] = None
    client.post(f"/api/browser-collection/items/{store_claim['id']}/store-discovery", json=discovery)
    product_claim = claim_item(client).json()["item"]
    assert product_claim["product_id"] != tracked["id"]
    client.post(
        f"/api/browser-collection/items/{product_claim['id']}/snapshot",
        json=product_payload("100500999002"),
    )

    rerun = client.post(
        "/api/browser-collection/runs/ensure",
        json={"target_date": today.isoformat(), "trigger": "MANUAL"},
    )

    assert rerun.status_code == 200
    assert rerun.json()["status"] == "RUNNING"
    pending = db_session.get(BrowserCollectionItem, product_claim["id"])
    assert pending.status == "PENDING"
    assert pending.attempt_count == 0
    assert db_session.query(BrowserCollectionItem).filter_by(target_type="PRODUCT").count() == 1


def test_manual_today_rerun_keeps_filter_test_items_skipped(client, db_session):
    store = create_store(client, "990129")
    product = create_product(client, store["id"], "100500991291")
    today = date.today()
    created = client.post(
        "/api/browser-collection/runs/ensure",
        json={"target_date": today.isoformat(), "trigger": "MANUAL"},
    )
    run = db_session.get(BrowserCollectionRun, created.json()["id"])
    store_item = db_session.query(BrowserCollectionItem).filter_by(target_type="STORE").one()
    store_item.status = "SUCCEEDED"
    store_item.completed_at = datetime.now(timezone.utc)
    skipped = BrowserCollectionItem(
        run_id=run.id,
        target_type="PRODUCT",
        target_key=f"product:{product['id']}",
        store_id=store["id"],
        product_id=product["id"],
        target_url=f"https://www.aliexpress.com/item/100500991291.html#monitor_product_id=100500991291",
        position=2,
        status="SKIPPED",
        completed_at=datetime.now(timezone.utc),
        error_type="queue_reduced_for_filter_test",
        error_message="Excluded from the limited accuracy-filter test batch.",
    )
    db_session.add(skipped)
    run.status = "PARTIAL"
    run.phase = "COMPLETED"
    run.completed_at = datetime.now(timezone.utc)
    db_session.commit()

    rerun = client.post(
        "/api/browser-collection/runs/ensure",
        json={"target_date": today.isoformat(), "trigger": "MANUAL"},
    )

    assert rerun.status_code == 200
    assert skipped.status == "SKIPPED"
    assert rerun.json()["pending_count"] == 0


def test_challenge_resume_preserves_the_claim_for_the_verified_store_tab(
    client, db_session
):
    store = create_store(client, "990103")
    create_product(client, store["id"], "100500993001")
    client.post(
        "/api/browser-collection/runs/ensure",
        json={"target_date": date.today().isoformat(), "trigger": "MANUAL"},
    )
    claim = claim_item(client).json()["item"]
    item = db_session.get(BrowserCollectionItem, claim["id"])
    item.claimed_at = datetime.now(timezone.utc) - timedelta(minutes=21)
    db_session.commit()
    paused = client.post(
        f"/api/browser-collection/items/{claim['id']}/challenge",
        json=add_claim_credentials({
            "error_message": "verification page",
            "page_url": "https://www.aliexpress.com/punish",
        }, claim["id"]),
    )
    assert paused.status_code == 200
    assert paused.json()["status"] == "NEEDS_VERIFICATION"

    resumed = client.post(f"/api/browser-collection/runs/{paused.json()['id']}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "RUNNING"
    current = resumed.json()["current_item"]
    assert current["id"] == claim["id"]
    assert current["status"] == "RUNNING"
    assert current["attempt_count"] == 1
    db_session.refresh(item)
    assert item.claimed_at.replace(tzinfo=timezone.utc) >= datetime.now(timezone.utc) - timedelta(seconds=1)

    heartbeat = client.post(
        "/api/browser-collection/heartbeat",
        json={
            "worker_id": _last_claim_credentials["worker_id"],
            "active_claims": [{
                "item_id": claim["id"],
                "claim_token": _last_claim_credentials["claim_token"],
            }],
        },
    )
    assert heartbeat.status_code == 200, heartbeat.text
    assert heartbeat.json()["current_item"]["id"] == claim["id"]
    assert request_claim(client).json()["item"] is None


def test_completing_challenged_page_clears_pause_reason(client):
    store = create_store(client, "990105")
    product = create_product(client, store["id"], "1005009910501")
    client.post(
        "/api/browser-collection/runs/ensure",
        json={"target_date": "2026-09-21", "trigger": "MANUAL"},
    )
    claim = claim_item(client).json()["item"]
    paused = client.post(
        f"/api/browser-collection/items/{claim['id']}/challenge",
        json=add_claim_credentials({"error_message": "verification page"}, claim["id"]),
    )
    assert paused.json()["paused_reason"] == "platform_challenge"

    completed = client.post(
        f"/api/browser-collection/items/{claim['id']}/store-discovery",
        json=monitored_store_payload("990105", product["aliexpress_product_id"], date.fromisoformat(claim["target_date"]), {}),
    )

    assert completed.status_code == 200
    assert completed.json()["status"] == "PARTIAL"
    assert completed.json()["paused_reason"] is None
    assert completed.json()["partial_count"] == 1


def test_manual_pause_stops_claiming_until_resumed_and_current_item_can_finish(client):
    store = create_store(client, "990106")
    product = create_product(client, store["id"], "100500993061")
    client.post(
        "/api/browser-collection/runs/ensure",
        json={"target_date": "2026-09-21", "trigger": "MANUAL"},
    )
    claim = claim_item(client).json()["item"]

    paused = client.post(f"/api/browser-collection/runs/{claim['run_id']}/pause")

    assert paused.status_code == 200
    assert paused.json()["status"] == "PAUSED"
    assert paused.json()["paused_reason"] == "manual_pause"
    assert request_claim(client).json()["item"] is None

    finished = client.post(
        f"/api/browser-collection/items/{claim['id']}/store-discovery",
        json=monitored_store_payload("990106", product["aliexpress_product_id"], date.fromisoformat(claim["target_date"]), {}),
    )
    assert finished.status_code == 200
    assert finished.json()["status"] == "PAUSED"

    resumed = client.post(f"/api/browser-collection/runs/{claim['run_id']}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "PARTIAL"
    assert resumed.json()["dispatch_enabled"] is False
    assert resumed.json()["pending_count"] == 0
    next_claim = request_claim(client).json()["item"]
    assert next_claim is None


def test_cancel_queue_skips_waiting_items_and_does_not_claim_next_item(client):
    stores = [create_store(client, store_id) for store_id in ("990161", "990162", "990163")]
    for index, store in enumerate(stores):
        create_product(client, store["id"], f"100500991610{index}")
    created = client.post(
        "/api/browser-collection/runs/ensure",
        json={"target_date": date.today().isoformat(), "trigger": "MANUAL"},
    )
    first = claim_item(client).json()["item"]

    cancelled = client.post(f"/api/browser-collection/runs/{created.json()['id']}/cancel")

    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "PAUSED"
    assert cancelled.json()["paused_reason"] == "manual_cancel"
    assert cancelled.json()["pending_count"] == 1
    statuses = {item["id"]: item["status"] for item in cancelled.json()["items"]}
    assert statuses[first["id"]] == "RUNNING"
    assert sum(status == "SKIPPED" for status in statuses.values()) == 2
    assert request_claim(client).json()["item"] is None

    finished = client.post(
        f"/api/browser-collection/items/{first['id']}/store-discovery",
        json=monitored_store_payload(
            stores[0]["aliexpress_store_id"], "1005009916100", date.fromisoformat(first["target_date"]), {}
        ),
    )

    assert finished.status_code == 200
    assert finished.json()["status"] == "PAUSED"
    assert finished.json()["paused_reason"] == "manual_cancel"
    assert finished.json()["partial_count"] == 1
    assert finished.json()["pending_count"] == 0
    assert request_claim(client).json()["item"] is None
    assert client.post(f"/api/browser-collection/runs/{created.json()['id']}/resume").status_code == 409


def test_cancel_verification_blocked_queue_and_resume_is_rejected(client):
    stores = [create_store(client, store_id) for store_id in ("990164", "990165")]
    for index, store in enumerate(stores):
        create_product(client, store["id"], f"100500991640{index}")
    created = client.post(
        "/api/browser-collection/runs/ensure",
        json={"target_date": date.today().isoformat(), "trigger": "MANUAL"},
    )
    claim_response = claim_item(client)
    claim = claim_response.json()["item"]
    challenge = client.post(
        f"/api/browser-collection/items/{claim['id']}/challenge",
        json=add_claim_credentials({"error_message": "verification page"}, claim["id"]),
    )

    cancelled = client.post(f"/api/browser-collection/runs/{created.json()['id']}/cancel")

    assert challenge.json()["status"] == "NEEDS_VERIFICATION"
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "PAUSED"
    assert cancelled.json()["paused_reason"] == "manual_cancel"
    assert cancelled.json()["pending_count"] == 0
    assert all(item["status"] == "SKIPPED" for item in cancelled.json()["items"])
    assert client.post(f"/api/browser-collection/runs/{created.json()['id']}/resume").status_code == 409
    assert request_claim(client).json()["item"] is None


def test_cancelled_daily_queue_allows_explicit_single_product_collection(client, db_session):
    store = create_store(client, "990166")
    product = create_product(client, store["id"], "100500991661")
    created = client.post(
        "/api/browser-collection/runs/ensure",
        json={"target_date": date.today().isoformat(), "trigger": "MANUAL"},
    )
    cancelled = client.post(f"/api/browser-collection/runs/{created.json()['id']}/cancel")

    selected = client.post(f"/api/products/{product['id']}/browser-collection")
    claim = claim_item(client).json()["item"]

    assert cancelled.json()["status"] == "PAUSED"
    assert selected.status_code == 200
    assert selected.json()["queued"] is True
    assert selected.json()["run"]["status"] == "PENDING"
    assert claim["target_type"] == "PRODUCT"
    assert claim["product_id"] == product["id"]
    old_store_item = db_session.query(BrowserCollectionItem).filter_by(target_type="STORE").one()
    assert old_store_item.status == "SKIPPED"


def test_manual_pause_preserves_pause_when_item_is_retried(client, db_session):
    store = create_store(client, "990108")
    create_product(client, store["id"], "100500993081")
    client.post(
        "/api/browser-collection/runs/ensure",
        json={"target_date": "2026-09-21", "trigger": "MANUAL"},
    )
    claim = claim_item(client).json()["item"]
    client.post(f"/api/browser-collection/runs/{claim['run_id']}/pause")
    item = db_session.get(BrowserCollectionItem, claim["id"])
    item.status = "FAILED"
    db_session.commit()

    retried = client.post(f"/api/browser-collection/items/{claim['id']}/retry")

    assert retried.status_code == 200
    assert retried.json()["status"] == "PAUSED"
    assert retried.json()["paused_reason"] == "manual_pause"


def test_retrying_failed_item_keeps_verification_blocked_queue_paused(client, db_session):
    first_store = create_store(client, "991201")
    second_store = create_store(client, "991202")
    create_product(client, first_store["id"], "1005009912011")
    create_product(client, second_store["id"], "1005009912021")
    run = client.post(
        "/api/browser-collection/runs/ensure", json={"trigger": "MANUAL"}
    ).json()
    first, second = db_session.query(BrowserCollectionItem).order_by(
        BrowserCollectionItem.position
    ).all()
    first.status = "NEEDS_VERIFICATION"
    first.error_type = "platform_challenge"
    first.error_message = "AliExpress 要求人工完成验证"
    second.status = "FAILED"
    second.error_type = "store_identity_mismatch"
    second.error_message = "店铺身份不匹配"
    run_row = db_session.get(BrowserCollectionRun, run["id"])
    run_row.status = "NEEDS_VERIFICATION"
    run_row.paused_reason = "platform_challenge"
    run_row.dispatch_enabled = False
    db_session.commit()

    response = client.post(f"/api/browser-collection/items/{second.id}/retry")

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "NEEDS_VERIFICATION"
    assert result["paused_reason"] == "platform_challenge"
    assert result["dispatch_enabled"] is False
    statuses = {item["id"]: item["status"] for item in result["items"]}
    assert statuses[first.id] == "NEEDS_VERIFICATION"
    assert statuses[second.id] == "PENDING"


def test_expired_lease_can_be_reclaimed(client, db_session):
    store = create_store(client, "990104")
    create_product(client, store["id"], "100500994001")
    client.post(
        "/api/browser-collection/runs/ensure",
        json={"target_date": "2026-09-21", "trigger": "MANUAL"},
    )
    first = claim_item(client).json()["item"]
    item = db_session.get(BrowserCollectionItem, first["id"])
    item.lease_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.commit()

    second = claim_item(client).json()["item"]
    assert second["id"] == first["id"]
    assert second["status"] == "RUNNING"
    assert second["attempt_count"] == 2


def test_heartbeat_renews_only_reported_running_item_leases(client, db_session):
    store = create_store(client, "990110")
    first_product = create_product(client, store["id"], "100500991101")
    second_product = create_product(client, store["id"], "100500991102")
    run = BrowserCollectionRun(
        target_date=date.today(),
        trigger="MANUAL",
        status="RUNNING",
        phase="PRODUCT_COLLECTION",
    )
    db_session.add(run)
    db_session.flush()
    for position, product in enumerate((first_product, second_product), start=1):
        db_session.add(
            BrowserCollectionItem(
                run_id=run.id,
                target_type="PRODUCT",
                target_key=f"product:{product['id']}:single",
                store_id=store["id"],
                product_id=product["id"],
                target_url=f"https://www.aliexpress.com/item/{product['aliexpress_product_id']}.html",
                position=position,
                status="PENDING",
                attempt_count=0,
            )
        )
    db_session.commit()
    first = claim_item(client).json()["item"]
    second = claim_item(client).json()["item"]
    first_row = db_session.get(BrowserCollectionItem, first["id"])
    second_row = db_session.get(BrowserCollectionItem, second["id"])
    renewed_from = datetime.now(timezone.utc) + timedelta(minutes=1)
    expired_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    first_row.lease_expires_at = renewed_from
    second_row.lease_expires_at = expired_at
    db_session.commit()

    response = client.post(
        "/api/browser-collection/heartbeat",
        json={
            "worker_id": "worker-test",
            "worker_label": "测试采集电脑",
            "extension_version": "test",
            "active_claims": [{"item_id": first["id"], "claim_token": first_row.claim_token}],
        },
    )

    assert response.status_code == 200
    db_session.refresh(first_row)
    db_session.refresh(second_row)
    first_lease = first_row.lease_expires_at.replace(tzinfo=timezone.utc)
    second_lease = second_row.lease_expires_at.replace(tzinfo=timezone.utc)
    assert first_lease > datetime.now(timezone.utc) + timedelta(minutes=4)
    assert second_lease == expired_at


def test_heartbeat_without_active_claims_renews_worker_owned_store_task(client, db_session):
    store = create_store(client, "990113")
    create_product(client, store["id"], "1005009911301")
    client.post(f"/api/stores/{store['id']}/browser-collection")
    claimed = claim_item(client).json()["item"]
    item = db_session.get(BrowserCollectionItem, claimed["id"])
    item.lease_expires_at = datetime.now(timezone.utc) + timedelta(seconds=30)
    db_session.commit()

    response = client.post(
        "/api/browser-collection/heartbeat",
        json={"worker_id": "worker-test", "extension_version": "0.6.112"},
    )

    assert response.status_code == 200
    db_session.refresh(item)
    assert item.status == "RUNNING"
    assert item.lease_expires_at.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc) + timedelta(minutes=4)


def test_heartbeat_rejects_stale_claim_without_renewing(client, db_session):
    store = create_store(client, "990137")
    create_product(client, store["id"], "1005009913701")
    client.post(
        "/api/browser-collection/runs/ensure",
        json={"target_date": "2026-09-21", "trigger": "MANUAL"},
    )
    claimed = claim_item(client).json()
    item = db_session.get(BrowserCollectionItem, claimed["item"]["id"])
    existing_lease = item.lease_expires_at

    response = client.post(
        "/api/browser-collection/heartbeat",
        json={
            "worker_id": "worker-test",
            "active_claims": [{"item_id": item.id, "claim_token": "stale-token"}],
        },
    )

    assert response.status_code == 409
    db_session.refresh(item)
    assert item.lease_expires_at == existing_lease


def test_heartbeat_requeues_an_item_that_exceeds_the_max_runtime(client, db_session):
    store = create_store(client, "990111")
    create_product(client, store["id"], "100500991111")
    client.post(
        "/api/browser-collection/runs/ensure",
        json={"target_date": "2026-09-21", "trigger": "MANUAL"},
    )
    claimed = claim_item(client).json()["item"]
    item = db_session.get(BrowserCollectionItem, claimed["id"])
    item.claimed_at = datetime.now(timezone.utc) - timedelta(minutes=21)
    item.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=4)
    db_session.commit()

    response = client.post(
        "/api/browser-collection/heartbeat",
        json={"worker_id": "worker-test", "active_claims": [{"item_id": claimed["id"], "claim_token": item.claim_token}]},
    )

    assert response.status_code == 200
    db_session.refresh(item)
    assert item.status == "PENDING"
    assert item.claimed_at is None
    assert item.lease_expires_at is None
    assert item.error_type == "browser_item_stalled"


def test_store_item_runtime_allows_longer_chart_batch(client, db_session):
    store = create_store(client, "990136")
    create_product(client, store["id"], "1005009913601")
    client.post(f"/api/stores/{store['id']}/browser-collection")
    claimed = claim_item(client).json()["item"]
    item = db_session.get(BrowserCollectionItem, claimed["id"])
    item.claimed_at = datetime.now(timezone.utc) - timedelta(minutes=13)
    item.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)
    db_session.commit()

    response = client.post(
        "/api/browser-collection/heartbeat",
        json={"worker_id": "worker-test", "active_claims": [{"item_id": claimed["id"], "claim_token": item.claim_token}]},
    )

    assert response.status_code == 200
    db_session.refresh(item)
    assert item.status == "RUNNING"
    assert item.lease_expires_at.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)


def test_product_item_runtime_remains_five_minutes(client, db_session):
    store = create_store(client, "990137")
    product = create_product(client, store["id"], "100500991137")
    client.post(f"/api/products/{product['id']}/browser-collection")
    claimed = claim_item(client).json()["item"]
    item = db_session.get(BrowserCollectionItem, claimed["id"])
    item.claimed_at = datetime.now(timezone.utc) - timedelta(minutes=5, seconds=30)
    item.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)
    db_session.commit()

    response = client.post(
        "/api/browser-collection/heartbeat",
        json={"worker_id": "worker-test", "active_claims": [{"item_id": claimed["id"], "claim_token": item.claim_token}]},
    )

    assert response.status_code == 200
    db_session.refresh(item)
    assert item.status == "PENDING"
    assert item.error_type == "browser_item_stalled"


def test_heartbeat_updates_counts_when_a_second_stalled_attempt_fails(client, db_session):
    store = create_store(client, "990112")
    create_product(client, store["id"], "100500991112")
    client.post(
        "/api/browser-collection/runs/ensure",
        json={"target_date": "2026-09-21", "trigger": "MANUAL"},
    )
    first = claim_item(client).json()["item"]
    item = db_session.get(BrowserCollectionItem, first["id"])
    item.status = "PENDING"
    item.claimed_at = None
    item.lease_expires_at = None
    db_session.commit()
    second = claim_item(client).json()["item"]
    item = db_session.get(BrowserCollectionItem, second["id"])
    item.claimed_at = datetime.now(timezone.utc) - timedelta(minutes=21)
    db_session.commit()

    response = client.post(
        "/api/browser-collection/heartbeat",
        json={"worker_id": "worker-test", "active_claims": [{"item_id": second["id"], "claim_token": item.claim_token}]},
    )

    assert response.status_code == 200
    assert response.json()["failed_count"] == 1
    db_session.refresh(item)
    assert item.status == "FAILED"
    assert item.error_type == "browser_item_stalled"


def test_pending_store_discovery_is_claimed_even_if_run_phase_was_advanced(client, db_session):
    store = create_store(client, "990106")
    create_product(client, store["id"], "1005009910603")
    client.post(
        "/api/browser-collection/runs/ensure",
        json={"target_date": "2026-09-21", "trigger": "MANUAL"},
    )
    run = db_session.query(BrowserCollectionRun).one()
    item = db_session.query(BrowserCollectionItem).one()
    run.phase = "PRODUCT_COLLECTION"
    db_session.commit()

    response = claim_item(client)

    assert response.status_code == 200
    assert response.json()["item"]["target_type"] == "STORE"
    assert response.json()["item"]["store_id"] == store["id"]
    assert response.json()["run"]["phase"] == "STORE_DISCOVERY"
    assert response.json()["item"]["status"] == "RUNNING"


def _history_candidate(product_id, target_date, values=None, *, complete=True):
    dates = [target_date - timedelta(days=offset) for offset in range(7, 0, -1)]
    supplied = values or [2, 0, 3, 1, 4, 0, 2]
    if not complete:
        supplied = supplied[:6]
    points = [
        {"date": metric_date.isoformat(), "value": value, "value_type": "daily_increment"}
        for metric_date, value in zip(dates, supplied)
    ]
    return {
        "platform_product_id": product_id,
        "url": f"https://www.aliexpress.com/item/{product_id}.html",
        "title": f"Candidate {product_id}",
        "historical_sales": points,
        "history_source": "ixspy_visible_daily_increment",
        "history_daily_capture_complete": complete,
        "history_expected_dates": [metric_date.isoformat() for metric_date in dates],
        "history_missing_dates": [] if complete else [dates[-1].isoformat()],
    }


def _daily_store_request(store_id, page_url, target_date, monitored_ids, candidate_page, histories=None):
    histories = histories or {}
    return {
        "platform_store_id": store_id,
        "url": page_url,
        "products": [
            {
                "platform_product_id": product_id,
                "url": f"https://www.aliexpress.com/item/{product_id}.html",
                "historical_sales": [
                    {"date": metric_date.isoformat(), "value": value, "value_type": "daily_increment"}
                    for metric_date, value in values.items()
                ],
                "history_source": "ixspy_visible_daily_increment",
            }
            for product_id, values in histories.items()
        ],
        "raw_data": {
            "collection_mode": "monitored_store_daily",
            "monitored_product_ids": sorted(monitored_ids),
            "visited_page_urls": [page_url],
            "history_target_date": target_date.isoformat(),
        },
        "candidate_page": candidate_page,
    }


def _candidate_page(page_url, store_id, candidates, *, scan_complete=True):
    return {
        "page_url": page_url,
        "pagination_known": True,
        "has_next_page": True,
        "next_page_url": (
            f"https://www.aliexpress.com/store/{store_id}/pages/all-items.html"
            "?page=2&sortType=bestmatch_sort&shop_sortType=orders_desc"
        ),
        "scan_complete": scan_complete,
        "candidates": candidates,
        "diagnostics": {"visible_product_count": len(candidates)},
    }


def test_underfilled_daily_store_adds_candidates_and_advances_cursor(client, db_session):
    store = create_store(client, "990301")
    tracked = [create_product(client, store["id"], f"1005009930{index:03d}") for index in range(19)]
    target_date = date.today()
    client.post("/api/browser-collection/runs/ensure", json={"target_date": target_date.isoformat()})
    claim = claim_item(client).json()["item"]
    page_url = claim["discovery_page_url"]
    low = _history_candidate("1005009931001", target_date, [1, 0, 1, 0, 1, 0, 1])
    high = _history_candidate("1005009931002", target_date, [8, 0, 7, 0, 6, 0, 5])
    low["orders_rank"] = 2
    high["orders_rank"] = 3
    candidate_page = _candidate_page(page_url, "990301", [low, high])
    candidate_page["diagnostics"]["candidate_progress_rejections"] = [{
        "product_id": "1005009931000",
        "orders_rank": 1,
        "reason": "history_checkpoint_invalid",
        "failed_checks": ["expected_dates"],
    }]
    payload = _daily_store_request(
        "990301", page_url, target_date,
        [product["aliexpress_product_id"] for product in tracked],
        candidate_page,
    )

    response = client.post(
        f"/api/browser-collection/items/{claim['id']}/store-discovery",
        json=add_claim_credentials(payload),
    )

    assert response.status_code == 200, response.text
    active_ids = {
        product.aliexpress_product_id
        for product in db_session.query(Product).filter_by(store_id=store["id"], status="active")
    }
    assert len(active_ids) == 20
    assert high["platform_product_id"] in active_ids
    assert low["platform_product_id"] not in active_ids
    enrolled = db_session.query(Product).filter_by(
        store_id=store["id"], aliexpress_product_id=high["platform_product_id"]
    ).one()
    assert enrolled.discovery_rank == 3
    state = db_session.get(StorePageDiscoveryState, store["id"])
    assert state.next_page_url.endswith("page=2&sortType=bestmatch_sort&shop_sortType=orders_desc")
    snapshot = db_session.query(StoreSnapshot).filter_by(store_id=store["id"]).one()
    assert snapshot.raw_payload["diagnostics"]["candidate_scan"]["added_ids"] == [
        high["platform_product_id"]
    ]
    skipped = snapshot.raw_payload["diagnostics"]["candidate_scan"]["skip_reasons"]
    assert skipped[0] == {
        "product_id": "1005009931000",
        "reason": "history_checkpoint_invalid",
        "failed_checks": ["expected_dates"],
        "orders_rank": 1,
    }


def test_incomplete_candidate_is_skipped_and_valid_page_advances(client, db_session):
    store = create_store(client, "990302")
    tracked = create_product(client, store["id"], "1005009932001")
    target_date = date.today()
    client.post("/api/browser-collection/runs/ensure", json={"target_date": target_date.isoformat()})
    claim = claim_item(client).json()["item"]
    page_url = claim["discovery_page_url"]
    incomplete = _history_candidate("1005009932002", target_date, complete=False)
    payload = _daily_store_request(
        "990302", page_url, target_date, [tracked["aliexpress_product_id"]],
        _candidate_page(page_url, "990302", [incomplete]),
    )

    response = client.post(
        f"/api/browser-collection/items/{claim['id']}/store-discovery",
        json=add_claim_credentials(payload),
    )

    assert response.status_code == 200, response.text
    assert db_session.query(Product).filter_by(store_id=store["id"], status="active").count() == 1
    state = db_session.get(StorePageDiscoveryState, store["id"])
    assert state.last_outcome == "advanced"
    snapshot = db_session.query(StoreSnapshot).filter_by(store_id=store["id"]).one()
    candidate_scan = snapshot.raw_payload["diagnostics"]["candidate_scan"]
    assert candidate_scan["skipped_ids"] == [incomplete["platform_product_id"]]


def test_candidate_scan_timeout_preserves_metrics_and_cursor(client, db_session):
    store = create_store(client, "990303")
    tracked = create_product(client, store["id"], "1005009933001")
    target_date = date.today()
    values = {
        target_date - timedelta(days=offset): value
        for offset, value in zip(range(1, 8), [2, 0, 3, 1, 4, 0, 2], strict=True)
    }
    client.post("/api/browser-collection/runs/ensure", json={"target_date": target_date.isoformat()})
    claim = claim_item(client).json()["item"]
    page_url = claim["discovery_page_url"]
    candidate = _history_candidate("1005009933002", target_date)
    payload = _daily_store_request(
        "990303", page_url, target_date, [tracked["aliexpress_product_id"]],
        _candidate_page(page_url, "990303", [candidate], scan_complete=False),
        {tracked["aliexpress_product_id"]: values},
    )

    response = client.post(
        f"/api/browser-collection/items/{claim['id']}/store-discovery",
        json=add_claim_credentials(payload),
    )

    assert response.status_code == 200, response.text
    metrics = db_session.query(ProductDailyMetric).filter_by(product_id=tracked["id"]).all()
    assert len(metrics) == 7
    assert all(metric.is_estimable for metric in metrics)
    assert db_session.query(Product).filter_by(store_id=store["id"]).count() == 1
    state = db_session.get(StorePageDiscoveryState, store["id"])
    assert state.next_page_url == page_url
    assert state.last_outcome == "incomplete"
    assert response.json()["items"][0]["status"] == "PARTIAL"


def test_empty_pool_daily_store_accepts_candidate_page(client, db_session):
    store = create_store(client, "990304")
    target_date = date.today()
    client.post("/api/browser-collection/runs/ensure", json={"target_date": target_date.isoformat()})
    claim = claim_item(client).json()["item"]
    page_url = claim["discovery_page_url"]
    candidate = _history_candidate("1005009934001", target_date)
    payload = _daily_store_request(
        "990304", page_url, target_date, [],
        _candidate_page(page_url, "990304", [candidate]),
    )

    response = client.post(
        f"/api/browser-collection/items/{claim['id']}/store-discovery",
        json=add_claim_credentials(payload),
    )

    assert response.status_code == 200, response.text
    assert db_session.query(Product).filter_by(store_id=store["id"], status="active").count() == 1
    metrics = db_session.query(ProductDailyMetric).filter(
        ProductDailyMetric.product_id == db_session.query(Product.id).filter_by(store_id=store["id"]).scalar()
    ).all()
    assert len(metrics) == 7


def test_daily_pool_fill_reactivates_only_reset_cleared_products(client, db_session):
    store = create_store(client, "990317")
    reset_cleared = create_product(client, store["id"], "1005009931701")
    manually_deactivated = create_product(client, store["id"], "1005009931702")
    reset_product = db_session.get(Product, reset_cleared["id"])
    reset_product.status = "inactive"
    reset_product.pool_reset_reactivation_eligible = True
    manual_product = db_session.get(Product, manually_deactivated["id"])
    manual_product.status = "inactive"
    db_session.commit()

    target_date = date.today()
    client.post("/api/browser-collection/runs/ensure", json={"target_date": target_date.isoformat()})
    claim = claim_item(client).json()["item"]
    page_url = claim["discovery_page_url"]
    reset_candidate = _history_candidate(reset_product.aliexpress_product_id, target_date)
    manual_candidate = _history_candidate(manual_product.aliexpress_product_id, target_date)
    payload = _daily_store_request(
        "990317", page_url, target_date, [],
        _candidate_page(page_url, "990317", [reset_candidate, manual_candidate]),
    )

    response = client.post(
        f"/api/browser-collection/items/{claim['id']}/store-discovery",
        json=add_claim_credentials(payload),
    )

    assert response.status_code == 200, response.text
    assert reset_product.status == "active"
    assert reset_product.pool_reset_reactivation_eligible is False
    assert manual_product.status == "inactive"
    snapshot = db_session.query(StoreSnapshot).filter_by(store_id=store["id"]).one()
    scan = snapshot.raw_payload["diagnostics"]["candidate_scan"]
    assert scan["added_ids"] == [reset_product.aliexpress_product_id]
    assert {
        item["product_id"]: item["reason"] for item in scan["skip_reasons"]
    } == {manual_product.aliexpress_product_id: "inactive_product_not_reactivated"}


def test_reset_cleared_candidate_requires_complete_daily_history(client, db_session):
    store = create_store(client, "990318")
    product_data = create_product(client, store["id"], "1005009931801")
    product = db_session.get(Product, product_data["id"])
    product.status = "inactive"
    product.pool_reset_reactivation_eligible = True
    db_session.commit()

    target_date = date.today()
    client.post("/api/browser-collection/runs/ensure", json={"target_date": target_date.isoformat()})
    claim = claim_item(client).json()["item"]
    page_url = claim["discovery_page_url"]
    incomplete = _history_candidate(product.aliexpress_product_id, target_date, complete=False)
    payload = _daily_store_request(
        "990318", page_url, target_date, [],
        _candidate_page(page_url, "990318", [incomplete]),
    )

    response = client.post(
        f"/api/browser-collection/items/{claim['id']}/store-discovery",
        json=add_claim_credentials(payload),
    )

    assert response.status_code == 200, response.text
    assert product.status == "inactive"
    assert product.pool_reset_reactivation_eligible is True
    snapshot = db_session.query(StoreSnapshot).filter_by(store_id=store["id"]).one()
    scan = snapshot.raw_payload["diagnostics"]["candidate_scan"]
    assert scan["skip_reasons"] == [{
        "product_id": product.aliexpress_product_id,
        "reason": "incomplete_or_cumulative_ixspy",
    }]


def test_candidate_page_with_unknown_pagination_fills_pool_without_wrapping_cursor(client, db_session):
    store = create_store(client, "990313")
    target_date = date.today()
    client.post("/api/browser-collection/runs/ensure", json={"target_date": target_date.isoformat()})
    claim = claim_item(client).json()["item"]
    page_url = claim["discovery_page_url"]
    candidate = _history_candidate("1005009931301", target_date)
    candidate_page = _candidate_page(page_url, "990313", [candidate])
    candidate_page.update(
        pagination_known=False,
        has_next_page=False,
        next_page_url=None,
    )
    payload = _daily_store_request("990313", page_url, target_date, [], candidate_page)

    response = client.post(
        f"/api/browser-collection/items/{claim['id']}/store-discovery",
        json=add_claim_credentials(payload),
    )

    assert response.status_code == 200, response.text
    assert db_session.query(Product).filter_by(
        store_id=store["id"], status="active", aliexpress_product_id=candidate["platform_product_id"]
    ).count() == 1
    state = db_session.get(StorePageDiscoveryState, store["id"])
    assert state.next_page_url == page_url
    assert state.last_outcome == "pagination_unknown"
    snapshot = db_session.query(StoreSnapshot).filter_by(store_id=store["id"]).one()
    candidate_scan = snapshot.raw_payload["diagnostics"]["candidate_scan"]
    assert candidate_scan["status"] == "pagination_unknown"
    assert candidate_scan["cursor_after"] == page_url


def test_full_pool_skips_candidate_page_without_advancing_cursor(client, db_session):
    store = create_store(client, "990305")
    tracked = [create_product(client, store["id"], f"1005009935{index:03d}") for index in range(20)]
    target_date = date.today()
    client.post("/api/browser-collection/runs/ensure", json={"target_date": target_date.isoformat()})
    claim = claim_item(client).json()["item"]
    page_url = claim["discovery_page_url"]
    candidate = _history_candidate("1005009935999", target_date)
    payload = _daily_store_request(
        "990305", page_url, target_date,
        [product["aliexpress_product_id"] for product in tracked],
        _candidate_page(page_url, "990305", [candidate]),
    )

    response = client.post(
        f"/api/browser-collection/items/{claim['id']}/store-discovery",
        json=add_claim_credentials(payload),
    )

    assert response.status_code == 200, response.text
    assert db_session.query(Product).filter_by(store_id=store["id"]).count() == 20
    assert db_session.get(StorePageDiscoveryState, store["id"]).next_page_url == page_url
    snapshot = db_session.query(StoreSnapshot).filter_by(store_id=store["id"]).one()
    assert snapshot.raw_payload["diagnostics"]["candidate_scan"]["status"] == "pool_full"


def test_candidate_page_url_limit_counts_unique_urls(client, db_session):
    store = create_store(client, "990306")
    target_date = date.today()
    client.post("/api/browser-collection/runs/ensure", json={"target_date": target_date.isoformat()})
    first = claim_item(client).json()["item"]
    first_url = first["discovery_page_url"]
    eleven_urls = [
        first_url,
        *[
            f"https://www.aliexpress.com/store/990306/pages/all-items.html"
            f"?page={page}&sortType=bestmatch_sort&shop_sortType=orders_desc"
            for page in range(2, 12)
        ],
    ]
    payload = _daily_store_request(
        "990306", first_url, target_date, [],
        _candidate_page(first_url, "990306", []),
    )
    payload["raw_data"]["visited_page_urls"] = eleven_urls + [first_url, first_url]
    accepted = client.post(
        f"/api/browser-collection/items/{first['id']}/store-discovery",
        json=add_claim_credentials(payload),
    )
    assert accepted.status_code == 200, accepted.text

    second_store = create_store(client, "990307")
    client.post(f"/api/stores/{second_store['id']}/browser-collection")
    second = claim_item(client).json()["item"]
    second_url = second["discovery_page_url"]
    twelve_urls = [
        second_url,
        *[
            f"https://www.aliexpress.com/store/990307/pages/all-items.html"
            f"?page={page}&sortType=bestmatch_sort&shop_sortType=orders_desc"
            for page in range(2, 13)
        ],
    ]
    too_many = _daily_store_request(
        "990307", second_url, target_date, [],
        _candidate_page(second_url, "990307", []),
    )
    too_many["raw_data"]["visited_page_urls"] = twelve_urls

    rejected = client.post(
        f"/api/browser-collection/items/{second['id']}/store-discovery",
        json=add_claim_credentials(too_many),
    )

    assert rejected.status_code == 409


def test_malformed_candidate_page_url_preserves_monitored_metrics(client, db_session):
    store = create_store(client, "990308")
    tracked = create_product(client, store["id"], "1005009938001")
    target_date = date.today()
    values = {
        target_date - timedelta(days=offset): value
        for offset, value in zip(range(1, 8), [2, 0, 3, 1, 4, 0, 2], strict=True)
    }
    client.post("/api/browser-collection/runs/ensure", json={"target_date": target_date.isoformat()})
    claim = claim_item(client).json()["item"]
    page_url = claim["discovery_page_url"]
    candidate_page = _candidate_page(page_url, "990308", [])
    candidate_page["page_url"] = "https://["
    payload = _daily_store_request(
        "990308", page_url, target_date, [tracked["aliexpress_product_id"]], candidate_page,
        {tracked["aliexpress_product_id"]: values},
    )

    response = client.post(
        f"/api/browser-collection/items/{claim['id']}/store-discovery",
        json=add_claim_credentials(payload),
    )

    assert response.status_code == 200, response.text
    metrics = db_session.query(ProductDailyMetric).filter_by(product_id=tracked["id"]).all()
    assert len(metrics) == 7
    assert all(metric.is_estimable for metric in metrics)
    assert db_session.get(StorePageDiscoveryState, store["id"]).next_page_url == page_url
    assert response.json()["items"][0]["status"] == "PARTIAL"


def _identity_review_payload(claim, page_url=None, *, route_store_id="1105179516"):
    return {
        "worker_id": "worker-test",
        "claim_token": claim["claim_token"],
        "page_url": page_url
        or f"https://www.aliexpress.com/store/{route_store_id}/pages/all-items.html?sortType=orders_desc",
        "identity_error_type": "store_identity_mismatch",
        "error_message": "页面身份与监控店铺信息不一致",
        "evidence": {
            "route_store_id": route_store_id,
            "heading_store_id": None,
            "labeled_store_ids": [],
            "page_title": "AMZSTEM Pop Toys Store - AliExpress",
            "ixspy_store_name": "AMZSTEM Pop Toys Store",
            "identity_method": "ixspy_store_name",
        },
    }


def _report_identity_review(client, claim, payload=None):
    return client.post(
        f"/api/browser-collection/items/{claim['item']['id']}/identity-review",
        json=payload or _identity_review_payload(claim),
    )


def test_store_identity_review_defers_with_structured_evidence(client, db_session):
    store = create_store(client, "990321")
    create_product(client, store["id"], "1005009932101")
    next_store = create_store(client, "9903211")
    create_product(client, next_store["id"], "1005009932111")
    claim = claim_item(client).json()

    response = _report_identity_review(client, claim)

    assert response.status_code == 200, response.text
    item = db_session.get(BrowserCollectionItem, claim["item"]["id"])
    assert item.status == "SKIPPED"
    assert item.error_type == "store_identity_deferred"
    assert item.identity_review_url == _identity_review_payload(claim)["page_url"]
    assert item.identity_review_evidence["identity_error_type"] == "store_identity_mismatch"
    assert item.identity_review_evidence["ixspy_store_name"] == "AMZSTEM Pop Toys Store"
    assert response.json()["status"] == "RUNNING"
    assert response.json()["paused_reason"] is None
    assert response.json()["dispatch_enabled"] is True
    deferred = next(
        item for item in response.json()["items"] if item["id"] == claim["item"]["id"]
    )
    assert deferred["identity_review_pending"] is True
    assert response.json()["current_item"] is None


def test_legacy_store_identity_review_does_not_block_queue_resume(client, db_session):
    first_store = create_store(client, "990327")
    create_product(client, first_store["id"], "1005009932701")
    next_store = create_store(client, "9903271")
    create_product(client, next_store["id"], "1005009932711")
    claim = claim_item(client).json()
    item = db_session.get(BrowserCollectionItem, claim["item"]["id"])
    evidence = _identity_review_payload(claim)
    item.status = "NEEDS_VERIFICATION"
    item.error_type = "store_identity_review"
    item.error_message = "店铺身份待人工审核"
    item.identity_review_url = evidence["page_url"]
    item.identity_review_evidence = evidence["evidence"]
    run = db_session.get(BrowserCollectionRun, item.run_id)
    run.status = "NEEDS_VERIFICATION"
    run.paused_reason = "store_identity_review"
    run.dispatch_enabled = False
    db_session.commit()

    response = client.post(f"/api/browser-collection/runs/{run.id}/resume")

    assert response.status_code == 200, response.text
    deferred = db_session.get(BrowserCollectionItem, item.id)
    assert deferred.status == "SKIPPED"
    assert deferred.error_type == "store_identity_deferred"
    assert deferred.identity_review_evidence["identity_review_decision"] == "pending"
    assert response.json()["status"] == "RUNNING"
    assert response.json()["dispatch_enabled"] is True
    assert request_claim(client).json()["item"]["store_id"] == next_store["id"]


def test_pending_store_identity_review_does_not_block_queue_resume(client, db_session):
    store = create_store(client, "990328")
    create_product(client, store["id"], "1005009932801")
    next_store = create_store(client, "9903281")
    create_product(client, next_store["id"], "1005009932811")
    claim = claim_item(client).json()
    assert _report_identity_review(client, claim).status_code == 200
    paused = client.post(
        f"/api/browser-collection/runs/{claim['item']['run_id']}/pause"
    )
    assert paused.status_code == 200, paused.text

    response = client.post(
        f"/api/browser-collection/runs/{claim['item']['run_id']}/resume"
    )

    assert response.status_code == 200
    item = db_session.get(BrowserCollectionItem, claim["item"]["id"])
    assert item.status == "SKIPPED"
    assert item.error_type == "store_identity_deferred"
    assert item.identity_review_url is not None
    run = db_session.get(BrowserCollectionRun, claim["item"]["run_id"])
    assert run.status == "RUNNING"
    assert run.dispatch_enabled is True


def test_confirm_store_identity_review_records_alias_without_resuming_current_item(
    client, db_session
):
    store = create_store(client, "990322")
    create_product(client, store["id"], "1005009932201")
    next_store = create_store(client, "9903221")
    create_product(client, next_store["id"], "1005009932211")
    claim = claim_item(client).json()
    assert _report_identity_review(client, claim).status_code == 200
    response = client.post(
        f"/api/browser-collection/items/{claim['item']['id']}/identity-review/confirm"
    )

    assert response.status_code == 200, response.text
    alias = db_session.query(StoreUrlIdentity).one()
    assert alias.store_id == store["id"]
    assert alias.route_store_id == "1105179516"
    assert alias.normalized_page_identity == (
        "www.aliexpress.com/store/1105179516/pages/all-items.html"
    )
    assert alias.source_item_id == claim["item"]["id"]
    assert alias.evidence["ixspy_store_name"] == "AMZSTEM Pop Toys Store"
    current = next(
        item for item in response.json()["items"] if item["id"] == claim["item"]["id"]
    )
    assert current["status"] == "SKIPPED"
    assert current["error_type"] == "store_identity_deferred"
    assert current["identity_review_decision"] == "confirmed"
    assert current["collection_start_url"] == (
        "https://www.aliexpress.com/store/1105179516/pages/all-items.html"
        "?sortType=bestmatch_sort&shop_sortType=orders_desc"
    )
    assert response.json()["status"] == "RUNNING"
    assert response.json()["paused_reason"] is None
    assert response.json()["dispatch_enabled"] is True


def test_identity_confirmation_does_not_resume_a_manually_paused_run(client, db_session):
    store = create_store(client, "9903222")
    create_product(client, store["id"], "1005009932221")
    next_store = create_store(client, "9903223")
    create_product(client, next_store["id"], "1005009932231")
    claim = claim_item(client).json()
    assert _report_identity_review(client, claim).status_code == 200
    paused = client.post(
        f"/api/browser-collection/runs/{claim['item']['run_id']}/pause"
    )
    assert paused.status_code == 200, paused.text

    confirmed = client.post(
        f"/api/browser-collection/items/{claim['item']['id']}/identity-review/confirm"
    )

    assert confirmed.status_code == 200, confirmed.text
    item = db_session.get(BrowserCollectionItem, claim["item"]["id"])
    run = db_session.get(BrowserCollectionRun, claim["item"]["run_id"])
    assert item.status == "SKIPPED"
    assert item.identity_review_evidence["identity_review_decision"] == "confirmed"
    assert run.status == "PAUSED"
    assert run.paused_reason == "manual_pause"
    assert run.dispatch_enabled is False


def test_identity_review_evidence_does_not_block_later_platform_challenge_resume(
    client,
):
    store = create_store(client, "990329")
    create_product(client, store["id"], "1005009932901")
    next_store = create_store(client, "9903291")
    create_product(client, next_store["id"], "1005009932911")
    claim = claim_item(client).json()
    assert _report_identity_review(client, claim).status_code == 200
    confirmed = client.post(
        f"/api/browser-collection/items/{claim['item']['id']}/identity-review/confirm"
    )
    assert confirmed.status_code == 200, confirmed.text

    next_claim = request_claim(client).json()
    assert next_claim["item"]["store_id"] == next_store["id"]
    challenge = client.post(
        f"/api/browser-collection/items/{next_claim['item']['id']}/challenge",
        json=add_claim_credentials({
            "error_message": "verification page",
            "page_url": "https://www.aliexpress.com/punish",
        }, next_claim["item"]["id"]),
    )
    assert challenge.status_code == 200, challenge.text
    assert challenge.json()["current_item"]["id"] == next_claim["item"]["id"]

    response = client.post(f"/api/browser-collection/runs/{claim['item']['run_id']}/resume")

    assert response.status_code == 200, response.text
    assert response.json()["current_item"]["status"] == "RUNNING"
    assert response.json()["current_item"]["id"] == next_claim["item"]["id"]


def test_store_collection_items_receive_only_their_store_approved_url_identities(
    client, db_session
):
    first_store = create_store(client, "990331")
    create_product(client, first_store["id"], "1005009933101")
    second_store = create_store(client, "990332")
    create_product(client, second_store["id"], "1005009933201")
    aliases = [
        StoreUrlIdentity(
            store_id=first_store["id"],
            route_store_id="1105179516",
            normalized_page_identity=(
                "www.aliexpress.com/store/1105179516/pages/all-items.html"
            ),
            first_observed_url=(
                "https://www.aliexpress.com/store/1105179516/pages/all-items.html"
            ),
            evidence={"ixspy_store_name": "AMZSTEM Pop Toys Store"},
        ),
        StoreUrlIdentity(
            store_id=second_store["id"],
            route_store_id="1105182474",
            normalized_page_identity=(
                "www.aliexpress.com/store/1105182474/pages/all-items.html"
            ),
            first_observed_url=(
                "https://www.aliexpress.com/store/1105182474/pages/all-items.html"
            ),
            evidence={"ixspy_store_name": "Other Store"},
        ),
    ]
    db_session.add_all(aliases)
    db_session.commit()
    client.post("/api/browser-collection/runs/ensure", json={"trigger": "MANUAL"})

    response = client.get("/api/browser-collection/runs/today")

    assert response.status_code == 200
    store_items = {
        item["store_id"]: item
        for item in response.json()["items"]
        if item["target_type"] == "STORE"
    }
    assert store_items[first_store["id"]]["approved_store_url_identities"] == [{
        "route_store_id": "1105179516",
        "normalized_page_identity": (
            "www.aliexpress.com/store/1105179516/pages/all-items.html"
        ),
        "evidence": {"ixspy_store_name": "AMZSTEM Pop Toys Store"},
    }]
    assert store_items[first_store["id"]]["collection_start_url"] == (
        "https://www.aliexpress.com/store/1105179516/pages/all-items.html"
        "?sortType=bestmatch_sort&shop_sortType=orders_desc"
    )
    assert store_items[second_store["id"]]["approved_store_url_identities"] == [{
        "route_store_id": "1105182474",
        "normalized_page_identity": (
            "www.aliexpress.com/store/1105182474/pages/all-items.html"
        ),
        "evidence": {"ixspy_store_name": "Other Store"},
    }]


def test_reject_store_identity_review_records_decision_without_alias_or_pause(
    client, db_session
):
    store = create_store(client, "990323")
    create_product(client, store["id"], "1005009932301")
    another_store = create_store(client, "9903231")
    create_product(client, another_store["id"], "1005009932311")
    claim = claim_item(client).json()
    assert _report_identity_review(client, claim).status_code == 200

    response = client.post(
        f"/api/browser-collection/items/{claim['item']['id']}/identity-review/reject"
    )

    assert response.status_code == 200, response.text
    item = db_session.get(BrowserCollectionItem, claim["item"]["id"])
    assert item.status == "SKIPPED"
    assert item.error_type == "store_identity_deferred"
    assert item.identity_review_evidence["identity_review_decision"] == "rejected"
    assert db_session.query(StoreUrlIdentity).count() == 0
    assert response.json()["status"] == "RUNNING"
    assert response.json()["paused_reason"] is None
    assert response.json()["dispatch_enabled"] is True
    assert response.json()["pending_count"] == 1
    repeated = client.post(
        f"/api/browser-collection/items/{claim['item']['id']}/identity-review/reject"
    )
    confirm_after_reject = client.post(
        f"/api/browser-collection/items/{claim['item']['id']}/identity-review/confirm"
    )
    assert repeated.status_code == 200, repeated.text
    assert confirm_after_reject.status_code == 409
    assert db_session.query(StoreUrlIdentity).count() == 0


def test_identity_confirmation_is_idempotent_and_rejects_cross_store_collision(
    client, db_session
):
    first_store = create_store(client, "990324")
    create_product(client, first_store["id"], "1005009932401")
    second_store = create_store(client, "990325")
    create_product(client, second_store["id"], "1005009932501")
    client.post("/api/browser-collection/runs/ensure", json={"trigger": "MANUAL"})
    first_claim = claim_item(client).json()
    first_review = _report_identity_review(client, first_claim)
    assert first_review.status_code == 200, first_review.text

    first_confirm = client.post(
        f"/api/browser-collection/items/{first_claim['item']['id']}/identity-review/confirm"
    )
    repeated_confirm = client.post(
        f"/api/browser-collection/items/{first_claim['item']['id']}/identity-review/confirm"
    )
    assert first_confirm.status_code == 200, first_confirm.text
    assert repeated_confirm.status_code == 200, repeated_confirm.text
    assert db_session.query(StoreUrlIdentity).count() == 1

    second_claim = request_claim(client).json()
    assert second_claim["item"]["store_id"] == second_store["id"]
    second_review = _report_identity_review(client, second_claim)
    assert second_review.status_code == 200, second_review.text

    collision = client.post(
        f"/api/browser-collection/items/{second_claim['item']['id']}/identity-review/confirm"
    )

    assert collision.status_code == 409
    assert db_session.query(StoreUrlIdentity).count() == 1
    second_item = db_session.get(BrowserCollectionItem, second_claim["item"]["id"])
    assert second_item.status == "SKIPPED"
    assert second_item.identity_review_evidence["identity_review_decision"] == "pending"


def test_identity_decision_rejects_stale_or_unclaimed_item(client, db_session):
    store = create_store(client, "990326")
    create_product(client, store["id"], "1005009932601")
    client.post("/api/browser-collection/runs/ensure", json={"trigger": "MANUAL"})
    item = db_session.query(BrowserCollectionItem).filter_by(target_type="STORE").one()

    response = client.post(
        f"/api/browser-collection/items/{item.id}/identity-review/confirm"
    )

    assert response.status_code == 409
    assert db_session.query(StoreUrlIdentity).count() == 0
    assert item.status == "PENDING"


def test_identity_review_rejects_url_evidence_mismatch(client, db_session):
    store = create_store(client, "990327")
    create_product(client, store["id"], "1005009932701")
    claim = claim_item(client).json()
    payload = _identity_review_payload(
        claim,
        "https://www.aliexpress.com/store/1105182474/pages/all-items.html",
        route_store_id="1105179516",
    )

    response = _report_identity_review(client, claim, payload)

    assert response.status_code == 409
    assert db_session.get(BrowserCollectionItem, claim["item"]["id"]).status == "RUNNING"
    assert db_session.query(StoreUrlIdentity).count() == 0


def test_identity_confirmation_does_not_require_online_selected_worker(
    client, db_session
):
    store = create_store(client, "990328")
    create_product(client, store["id"], "1005009932801")
    next_store = create_store(client, "9903282")
    create_product(client, next_store["id"], "1005009932821")
    claim = claim_item(client).json()
    assert _report_identity_review(client, claim).status_code == 200
    worker = db_session.get(BrowserCollectionWorker, "worker-test")
    worker.last_heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    db_session.commit()

    response = client.post(
        f"/api/browser-collection/items/{claim['item']['id']}/identity-review/confirm"
    )

    assert response.status_code == 200, response.text
    assert db_session.query(StoreUrlIdentity).count() == 1
    item = db_session.get(BrowserCollectionItem, claim["item"]["id"])
    run = db_session.get(BrowserCollectionRun, claim["item"]["run_id"])
    assert item.status == "SKIPPED"
    assert item.error_type == "store_identity_deferred"
    assert item.identity_review_evidence["identity_review_decision"] == "confirmed"
    assert run.status == "RUNNING"
    assert run.paused_reason is None
    assert run.dispatch_enabled is True


def test_platform_challenge_cannot_use_identity_decisions(client, db_session):
    store = create_store(client, "990329")
    create_product(client, store["id"], "1005009932901")
    claim = claim_item(client).json()
    challenge = client.post(
        f"/api/browser-collection/items/{claim['item']['id']}/challenge",
        json={
            "worker_id": "worker-test",
            "claim_token": claim["claim_token"],
            "error_message": "AliExpress CAPTCHA",
            "page_url": "https://www.aliexpress.com/punish",
        },
    )
    assert challenge.status_code == 200, challenge.text

    report = _report_identity_review(client, claim)
    confirm = client.post(
        f"/api/browser-collection/items/{claim['item']['id']}/identity-review/confirm"
    )
    reject = client.post(
        f"/api/browser-collection/items/{claim['item']['id']}/identity-review/reject"
    )

    assert report.status_code == 409
    assert confirm.status_code == 409
    assert reject.status_code == 409
    assert challenge.json()["paused_reason"] == "platform_challenge"
    assert db_session.query(StoreUrlIdentity).count() == 0


def test_store_identity_review_skips_only_current_store_and_claims_next(
    client, db_session
):
    first_store = create_store(client, "990341")
    create_product(client, first_store["id"], "1005009934101")
    second_store = create_store(client, "990342")
    create_product(client, second_store["id"], "1005009934201")
    first_claim = claim_item(client).json()

    reviewed = _report_identity_review(client, first_claim)
    first_item = db_session.get(BrowserCollectionItem, first_claim["item"]["id"])
    second_claim = request_claim(client).json()

    assert reviewed.status_code == 200, reviewed.text
    assert first_item.status == "SKIPPED"
    assert first_item.error_type == "store_identity_deferred"
    assert second_claim["item"]["store_id"] == second_store["id"]
    assert reviewed.json()["dispatch_enabled"] is True
    assert reviewed.json()["status"] == "RUNNING"


def test_store_identity_review_preserves_evidence_and_clears_claim(client, db_session):
    store = create_store(client, "990343")
    create_product(client, store["id"], "1005009934301")
    claim = claim_item(client).json()
    payload = _identity_review_payload(claim)

    response = _report_identity_review(client, claim, payload)

    assert response.status_code == 200, response.text
    item = db_session.get(BrowserCollectionItem, claim["item"]["id"])
    assert item.status == "SKIPPED"
    assert item.identity_review_url == payload["page_url"]
    assert item.identity_review_evidence["ixspy_store_name"] == "AMZSTEM Pop Toys Store"
    assert item.identity_review_evidence["identity_review_decision"] == "pending"
    assert item.claimed_at is None
    assert item.lease_expires_at is None
    assert item.claimed_worker_id is None
    assert item.claim_token is None


def test_store_identity_review_does_not_mutate_another_legacy_claim(client, db_session):
    first_store = create_store(client, "990344")
    create_product(client, first_store["id"], "1005009934401")
    second_store = create_store(client, "990345")
    create_product(client, second_store["id"], "1005009934501")
    third_store = create_store(client, "990346")
    create_product(client, third_store["id"], "1005009934601")
    first_claim = claim_item(client).json()
    run_id = first_claim["item"]["run_id"]
    second = (
        db_session.query(BrowserCollectionItem)
        .filter_by(run_id=run_id, target_type="STORE", position=2)
        .one()
    )
    second.status = "RUNNING"
    second.claimed_worker_id = "worker-test"
    second.claim_token = "legacy-running-claim"
    second.claimed_at = datetime.now(timezone.utc)
    second.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    db_session.commit()

    response = _report_identity_review(client, first_claim)
    third_claim = request_claim(client).json()

    assert response.status_code == 200, response.text
    db_session.refresh(second)
    assert second.status == "RUNNING"
    assert second.claim_token == "legacy-running-claim"
    assert third_claim["item"]["store_id"] == third_store["id"]
    assert third_claim["item"]["id"] != second.id


def test_store_discovery_claim_allows_multiple_running_stores_per_run(client, db_session):
    from app.services.browser_collection import finish_item

    first_store = create_store(client, "990347")
    create_product(client, first_store["id"], "1005009934701")
    second_store = create_store(client, "990348")
    create_product(client, second_store["id"], "1005009934801")
    first_claim = claim_item(client).json()

    concurrent_claim = request_claim(client).json()

    assert concurrent_claim["item"] is not None
    assert concurrent_claim["item"]["id"] != first_claim["item"]["id"]
    assert concurrent_claim["item"]["store_id"] != first_claim["item"]["store_id"]
    first_item = db_session.get(BrowserCollectionItem, first_claim["item"]["id"])
    finish_item(db_session, first_item, "SUCCEEDED")
    run = db_session.get(BrowserCollectionRun, first_item.run_id)
    db_session.refresh(run)
    assert run.phase == "STORE_DISCOVERY"
    assert run.status == "RUNNING"
    next_claim = request_claim(client).json()
    assert next_claim["item"] is None


def test_terminal_store_failure_does_not_pause_dispatch(client, db_session):
    first_store = create_store(client, "990349")
    create_product(client, first_store["id"], "1005009934901")
    second_store = create_store(client, "990350")
    create_product(client, second_store["id"], "1005009935001")
    first_claim = claim_item(client).json()
    first_item_id = first_claim["item"]["id"]

    first_failure = client.post(
        f"/api/browser-collection/items/{first_item_id}/failure",
        json=add_claim_credentials({
            "error_type": "page_not_ready",
            "error_message": "店铺页未就绪",
        }, first_item_id),
    )
    assert first_failure.json()["status"] == "RUNNING"
    retry_claim = claim_item(client).json()
    assert retry_claim["item"]["id"] == first_item_id
    terminal_failure = client.post(
        f"/api/browser-collection/items/{first_item_id}/failure",
        json=add_claim_credentials({
            "error_type": "page_not_ready",
            "error_message": "店铺页未就绪",
        }, first_item_id),
    )

    assert terminal_failure.status_code == 200
    assert terminal_failure.json()["status"] == "RUNNING"
    assert terminal_failure.json()["paused_reason"] is None
    assert terminal_failure.json()["dispatch_enabled"] is True
    assert terminal_failure.json()["pending_count"] == 1
    assert request_claim(client).json()["item"]["store_id"] == second_store["id"]


def test_partial_store_result_keeps_dispatching_next_store(client, db_session):
    first_store = create_store(client, "990351")
    first_product = create_product(client, first_store["id"], "1005009935101")
    second_store = create_store(client, "990352")
    create_product(client, second_store["id"], "1005009935201")
    claim = claim_item(client).json()

    response = client.post(
        f"/api/browser-collection/items/{claim['item']['id']}/store-discovery",
        json=monitored_store_payload(
            first_store["aliexpress_store_id"],
            first_product["aliexpress_product_id"],
            date.fromisoformat(claim["item"]["target_date"]),
            {},
        ),
    )

    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["status"] == "PARTIAL"
    assert response.json()["status"] == "RUNNING"
    assert response.json()["paused_reason"] is None
    assert response.json()["dispatch_enabled"] is True
    assert response.json()["pending_count"] == 1
    assert request_claim(client).json()["item"]["store_id"] == second_store["id"]


def test_store_discovery_identity_validation_becomes_deferred_without_snapshot(
    client, db_session
):
    store = create_store(client, "990353")
    tracked = create_product(client, store["id"], "1005009935301")
    next_store = create_store(client, "990354")
    create_product(client, next_store["id"], "1005009935401")
    target_date = date.today()
    claim = claim_item(client).json()
    item_id = claim["item"]["id"]
    discovery_state = db_session.get(StorePageDiscoveryState, store["id"])
    original_cursor = discovery_state.next_page_url
    observed_url = (
        "https://www.aliexpress.com/store/990999/pages/all-items.html"
        "?sortType=bestmatch_sort&shop_sortType=orders_desc"
    )
    payload = monitored_store_payload(
        "990353", tracked["aliexpress_product_id"], target_date, {}
    )
    payload["url"] = observed_url
    payload["raw_data"]["visited_page_urls"] = [observed_url]
    payload["raw_data"]["store_identity"] = {
        "route_store_id": "990999",
        "heading_store_id": "990999",
        "labeled_store_ids": ["990999"],
        "page_title": "Observed page title",
        "ixspy_store_name": "Observed IXSPY Store",
        "identity_method": "ixspy_store_name",
    }

    response = client.post(
        f"/api/browser-collection/items/{item_id}/store-discovery",
        json=add_claim_credentials(payload, item_id),
    )

    assert response.status_code == 200, response.text
    item = db_session.get(BrowserCollectionItem, item_id)
    assert item.status == "SKIPPED"
    assert item.error_type == "store_identity_deferred"
    assert item.identity_review_url == observed_url
    assert item.identity_review_evidence["route_store_id"] == "990999"
    assert item.identity_review_evidence["ixspy_store_name"] == "Observed IXSPY Store"
    assert db_session.query(StoreSnapshot).count() == 0
    assert db_session.query(ProductSnapshot).count() == 0
    assert db_session.query(ProductDailyMetric).count() == 0
    assert db_session.get(StorePageDiscoveryState, store["id"]).next_page_url == original_cursor
    assert response.json()["dispatch_enabled"] is True
    assert response.json()["status"] == "RUNNING"


def test_identity_confirm_records_alias_without_resuming_or_requeueing(
    client, db_session
):
    store = create_store(client, "990358")
    create_product(client, store["id"], "1005009935801")
    next_store = create_store(client, "990359")
    create_product(client, next_store["id"], "1005009935901")
    claim = claim_item(client).json()
    assert _report_identity_review(client, claim).status_code == 200
    item_id = claim["item"]["id"]
    run_id = claim["item"]["run_id"]
    run = db_session.get(BrowserCollectionRun, run_id)
    before = (run.status, run.paused_reason, run.dispatch_enabled)

    response = client.post(
        f"/api/browser-collection/items/{item_id}/identity-review/confirm"
    )

    assert response.status_code == 200, response.text
    item = db_session.get(BrowserCollectionItem, item_id)
    alias = db_session.query(StoreUrlIdentity).one()
    assert alias.store_id == store["id"]
    assert alias.source_item_id == item_id
    assert item.status == "SKIPPED"
    assert item.error_type == "store_identity_deferred"
    assert item.identity_review_evidence["identity_review_decision"] == "confirmed"
    assert response.json()["items"][0]["identity_review_pending"] is False
    assert (run.status, run.paused_reason, run.dispatch_enabled) == before


def test_identity_reject_records_decision_without_alias_or_pausing_queue(
    client, db_session
):
    store = create_store(client, "990360")
    create_product(client, store["id"], "1005009936001")
    next_store = create_store(client, "990361")
    create_product(client, next_store["id"], "1005009936101")
    claim = claim_item(client).json()
    assert _report_identity_review(client, claim).status_code == 200
    run = db_session.get(BrowserCollectionRun, claim["item"]["run_id"])

    response = client.post(
        f"/api/browser-collection/items/{claim['item']['id']}/identity-review/reject"
    )

    assert response.status_code == 200, response.text
    item = db_session.get(BrowserCollectionItem, claim["item"]["id"])
    assert item.status == "SKIPPED"
    assert item.error_type == "store_identity_deferred"
    assert item.identity_review_evidence["identity_review_decision"] == "rejected"
    assert db_session.query(StoreUrlIdentity).count() == 0
    assert run.status == "RUNNING"
    assert run.paused_reason is None
    assert run.dispatch_enabled is True


def test_only_confirmed_identity_deferred_item_can_be_requeued(client, db_session):
    from app.services.browser_collection import finish_item

    stores = []
    for store_id, product_id in [
        ("990362", "1005009936201"),
        ("990363", "1005009936301"),
        ("990364", "1005009936401"),
    ]:
        store = create_store(client, store_id)
        create_product(client, store["id"], product_id)
        stores.append(store)

    first_claim = claim_item(client).json()
    first_id = first_claim["item"]["id"]
    assert _report_identity_review(
        client, first_claim, _identity_review_payload(first_claim, route_store_id="1105179517")
    ).status_code == 200
    pending_retry = client.post(f"/api/browser-collection/items/{first_id}/retry")
    assert pending_retry.status_code == 409
    confirmed = client.post(
        f"/api/browser-collection/items/{first_id}/identity-review/confirm"
    )
    assert confirmed.status_code == 200, confirmed.text
    before_dispatch = confirmed.json()["dispatch_enabled"]
    requeued = client.post(f"/api/browser-collection/items/{first_id}/retry")
    assert requeued.status_code == 200, requeued.text
    first_item = db_session.get(BrowserCollectionItem, first_id)
    assert first_item.status == "PENDING"
    assert first_item.identity_review_evidence["identity_review_decision"] == "confirmed"
    assert requeued.json()["dispatch_enabled"] == before_dispatch

    finish_item(db_session, first_item, "SUCCEEDED")
    second_claim = claim_item(client).json()
    second_id = second_claim["item"]["id"]
    assert second_claim["item"]["store_id"] == stores[1]["id"]
    assert _report_identity_review(
        client, second_claim, _identity_review_payload(second_claim, route_store_id="1105179518")
    ).status_code == 200
    assert client.post(
        f"/api/browser-collection/items/{second_id}/identity-review/reject"
    ).status_code == 200
    assert client.post(f"/api/browser-collection/items/{second_id}/retry").status_code == 409

    third_item = (
        db_session.query(BrowserCollectionItem)
        .filter_by(run_id=first_claim["item"]["run_id"], store_id=stores[2]["id"])
        .one()
    )
    third_item.status = "SKIPPED"
    third_item.error_type = "queue_cancelled"
    third_item.completed_at = datetime.now(timezone.utc)
    db_session.commit()
    assert client.post(
        f"/api/browser-collection/items/{third_item.id}/retry"
    ).status_code == 409


def test_confirmed_deferred_item_can_reuse_an_existing_store_url_identity(
    client, db_session
):
    store = create_store(client, "990365")
    create_product(client, store["id"], "1005009936501")
    alias_url = "https://www.aliexpress.com/store/1105179516/pages/all-items.html?shop_sortType=orders_desc"
    db_session.add(StoreUrlIdentity(
        store_id=store["id"],
        route_store_id="1105179516",
        normalized_page_identity=(
            "www.aliexpress.com/store/1105179516/pages/all-items.html"
        ),
        first_observed_url=alias_url,
        evidence={"ixspy_store_name": "AMZSTEM Pop Toys Store"},
    ))
    db_session.commit()
    claim = claim_item(client).json()
    assert _report_identity_review(client, claim).status_code == 200

    confirmed = client.post(
        f"/api/browser-collection/items/{claim['item']['id']}/identity-review/confirm"
    )
    assert confirmed.status_code == 200, confirmed.text
    requeued = client.post(
        f"/api/browser-collection/items/{claim['item']['id']}/retry"
    )

    assert requeued.status_code == 200, requeued.text
    item = db_session.get(BrowserCollectionItem, claim["item"]["id"])
    assert item.status == "PENDING"
    assert item.identity_review_evidence["identity_review_decision"] == "confirmed"
    requeued_item = next(
        queued for queued in requeued.json()["items"] if queued["id"] == item.id
    )
    assert requeued_item["collection_start_url"] == (
        "https://www.aliexpress.com/store/1105179516/pages/all-items.html"
        "?sortType=bestmatch_sort&shop_sortType=orders_desc"
    )
    assert "/store/990365/pages/all-items.html" in requeued_item["target_url"]
