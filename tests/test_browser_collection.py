from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pytest

from app.db.models import (
    BrowserCollectionItem,
    BrowserCollectionRun,
    Product,
    ProductDailyMetric,
    ProductSnapshot,
    Store,
    StorePageDiscoveryState,
    StoreSnapshot,
)
from app.services.browser_collection import _products_with_recent_history_for_date
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
    return client.post(
        "/api/browser-collection/items/claim", json={"worker_id": worker_id}
    )


def test_scheduled_run_is_created_held(client):
    store = create_store(client, "990201")
    create_product(client, store["id"], "1005009920101")

    response = client.post(
        "/api/browser-collection/runs/ensure", json={"trigger": "SCHEDULED"}
    )

    assert response.status_code == 200
    assert response.json()["dispatch_enabled"] is False
    assert response.json()["pending_count"] == 1


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


def test_only_selected_online_worker_can_claim(client):
    store = create_store(client, "990203")
    create_product(client, store["id"], "1005009920301")
    register_browser_worker(client, "worker-galore", "Galore主机")
    register_browser_worker(client, "worker-second", "第二台电脑")
    assert select_browser_worker(client, "worker-galore").status_code == 200
    assert client.post("/api/browser-collection/runs/start-today").status_code == 200

    other = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-second"}
    )
    selected = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-galore"}
    )

    assert other.status_code == 200
    assert other.json()["item"] is None
    assert selected.status_code == 200
    assert selected.json()["item"]["target_type"] == "STORE"


def test_non_selected_heartbeat_cannot_change_owner(client):
    register_browser_worker(client, "worker-galore", "Galore主机")
    register_browser_worker(client, "worker-second", "第二台电脑")
    assert select_browser_worker(client, "worker-galore").status_code == 200

    register_browser_worker(client, "worker-second", "第二台电脑")
    control = client.get("/api/browser-collection/control")

    assert control.status_code == 200
    assert control.json()["selected_worker"]["worker_id"] == "worker-galore"


def test_selected_worker_offline_does_not_fail_over(client, monkeypatch):
    from app.services import browser_collection as browser_service

    register_browser_worker(client, "worker-galore", "Galore主机")
    assert select_browser_worker(client, "worker-galore").status_code == 200
    register_browser_worker(client, "worker-second", "第二台电脑")
    stale_now = datetime.now(timezone.utc) - timedelta(minutes=3)
    monkeypatch.setattr(browser_service, "utcnow", lambda: stale_now)
    register_browser_worker(client, "worker-galore", "Galore主机")

    start = client.post("/api/browser-collection/runs/start-today")
    claim = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-second"}
    )
    control = client.get("/api/browser-collection/control")

    assert start.status_code == 409
    assert claim.status_code == 200
    assert claim.json()["item"] is None
    assert control.json()["selected_worker"]["worker_id"] == "worker-galore"


def test_handoff_disables_dispatch_until_old_claims_drain(client, db_session):
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
    blocked_start = client.post("/api/browser-collection/runs/start-today")
    old_claim = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-galore"}
    )
    assert switched.status_code == 200
    assert switched.json()["dispatch_enabled"] is False
    assert blocked_start.status_code == 409
    assert old_claim.json()["item"] is None

    old_item = db_session.get(BrowserCollectionItem, first["id"])
    old_item.status = "SUCCEEDED"
    old_item.completed_at = datetime.now(timezone.utc)
    old_item.lease_expires_at = None
    db_session.commit()
    started = client.post("/api/browser-collection/runs/start-today")
    next_claim = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-second"}
    )

    assert started.status_code == 200
    assert next_claim.status_code == 200
    assert next_claim.json()["item"]["id"] != first["id"]


def test_concurrent_selected_worker_claims_are_disjoint(client):
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

    assert first is not None and second is not None
    assert first["id"] != second["id"]


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


def test_handoff_drains_old_claim_and_rejects_stale_result(client, db_session):
    store = create_store(client, "990209")
    product = create_product(client, store["id"], "1005009920901")
    register_browser_worker(client, "worker-galore", "Galore主机")
    register_browser_worker(client, "worker-second", "第二台电脑")
    assert select_browser_worker(client, "worker-galore").status_code == 200
    assert client.post("/api/browser-collection/runs/start-today").status_code == 200
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

    retry = client.post(f"/api/browser-collection/items/{first['id']}/retry")
    assert retry.status_code == 200
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


def test_store_claim_contains_only_active_monitored_ids(client, db_session):
    selected = create_store(client, "990106")
    other = create_store(client, "990107")
    create_product(client, selected["id"], "1005009910602")
    inactive = create_product(client, selected["id"], "1005009910601")
    create_product(client, other["id"], "1005009910701")
    db_session.get(Product, inactive["id"]).status = "inactive"
    db_session.commit()
    client.post("/api/browser-collection/runs/ensure", json={"trigger": "MANUAL"})

    claim = claim_item(client).json()["item"]

    assert claim["store_id"] == selected["id"]
    assert claim["monitored_product_ids"] == ["1005009910602"]


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

    assert response.status_code == 409
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


def test_challenge_pauses_and_resume_releases_the_item(client):
    store = create_store(client, "990103")
    create_product(client, store["id"], "100500993001")
    client.post(
        "/api/browser-collection/runs/ensure",
        json={"target_date": date.today().isoformat(), "trigger": "MANUAL"},
    )
    claim = claim_item(client).json()["item"]
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
    assert resumed.json()["current_item"] is None


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
    item.claimed_at = datetime.now(timezone.utc) - timedelta(minutes=13)
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
    item.claimed_at = datetime.now(timezone.utc) - timedelta(minutes=5, seconds=30)
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
    item.claimed_at = datetime.now(timezone.utc) - timedelta(minutes=13)
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
