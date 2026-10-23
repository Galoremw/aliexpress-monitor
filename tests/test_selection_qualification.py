from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.db.models import (
    BrowserCollectionItem,
    DianxiaomiHandoff,
    Product,
    ProductDailyMetric,
    Store,
)


@pytest.fixture
def service_tokens(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setenv("SELECTION_SERVICE_TOKEN", "selection-test-secret")
    monkeypatch.setenv("DIANXIAOMI_SERVICE_TOKEN", "dianxiaomi-test-secret")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def selection_headers():
    return {"Authorization": "Bearer selection-test-secret"}


def dianxiaomi_headers():
    return {"Authorization": "Bearer dianxiaomi-test-secret"}


def import_one(client, batch_id="pilot-2026-10-21", product_id="100500990"):
    return client.post(
        "/api/integrations/selection/candidates",
        headers=selection_headers(),
        json={
            "batch_id": batch_id,
            "candidates": [
                {
                    "aliexpress_product_id": product_id,
                    "url": f"https://www.aliexpress.com/item/{product_id}.html",
                    "title": "Workbench candidate",
                    "source_candidate_id": f"source-{product_id}",
                }
            ],
        },
    )


def record_daily_values(db_session, product_id, values, *, end_date=None):
    end_date = end_date or (
        datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)
    )
    for offset, value in enumerate(values):
        db_session.add(
            ProductDailyMetric(
                product_id=product_id,
                metric_date=end_date - timedelta(days=6 - offset),
                estimated_sales=value,
                is_estimable=True,
                reason="ixspy_visible_daily_increment",
                calculation_method="ixspy_visible_daily_increment",
            )
        )
    db_session.commit()
    return end_date


def test_selection_intake_deduplicates_across_batches_and_preserves_provenance(
    client, db_session, service_tokens
):
    first = import_one(client)
    assert first.status_code == 201
    assert first.json()["items"][0]["batch_association_created"] is True
    product_id = first.json()["items"][0]["product_id"]

    repeated_batch = import_one(client)
    second_batch = import_one(client, batch_id="pilot-2026-10-22")

    assert repeated_batch.status_code == 200
    assert repeated_batch.json()["items"][0]["batch_association_created"] is False
    assert second_batch.status_code == 200
    assert second_batch.json()["items"][0]["batch_association_created"] is True
    assert second_batch.json()["items"][0]["product_id"] == product_id
    product = db_session.get(Product, product_id)
    assert product.monitor_scope == "selection_candidate"
    assert db_session.query(Product).filter_by(aliexpress_product_id="100500990").count() == 1
    assert client.get("/api/products").json() == []
    assert len(client.get("/api/products?scope=selection_candidate").json()) == 1
    assert db_session.query(DianxiaomiHandoff).count() == 0

    candidates = client.get("/api/selection-qualification/candidates").json()
    candidate = candidates[0]
    assert candidate["aliexpress_product_id"] == "100500990"
    assert {item["batch_id"] for item in candidate["provenance"]} == {
        "pilot-2026-10-21",
        "pilot-2026-10-22",
    }
    assert len(candidate["provenance"]) == 2


def test_existing_competitor_product_becomes_selection_scope_and_gets_dedicated_handoff(
    client, db_session, service_tokens
):
    store = Store(name="Existing competitor", url="https://www.aliexpress.com/store/556677")
    product = Product(
        store=store,
        aliexpress_product_id="100500997",
        url="https://www.aliexpress.us/us/item/100500997.html?spm=old-listing",
        title="Existing competitor listing",
        status="inactive",
    )
    db_session.add(product)
    db_session.flush()
    old_handoff = DianxiaomiHandoff(
        batch_id="manual-existing-batch",
        product_id=product.id,
        store_id=store.id,
        target_url=product.url,
        status="QUEUED",
    )
    db_session.add(old_handoff)
    db_session.commit()
    product_pk = product.id
    old_handoff_pk = old_handoff.id
    original_store_id = store.id

    imported = import_one(client, batch_id="selected-existing-product", product_id="100500997")
    assert imported.status_code == 200
    assert imported.json()["items"][0]["product_id"] == product_pk
    persisted_product = db_session.get(Product, product_pk)
    assert persisted_product.monitor_scope == "selection_candidate"
    assert persisted_product.status == "active"
    assert persisted_product.store_id == original_store_id
    collection = client.post(f"/api/products/{product_pk}/browser-collection")
    assert collection.status_code == 200
    assert collection.json()["item"]["target_url"] == (
        "https://www.aliexpress.com/item/100500997.html#monitor_product_id=100500997"
    )

    record_daily_values(db_session, product_pk, [1, 1, 1, 1, 1, 1, 1])
    client.put(
        "/api/selection-qualification/rule",
        json={"minimum_7d_increment_total": 7, "minimum_positive_days": 7},
    )
    candidate = client.get("/api/selection-qualification/candidates").json()[0]
    assert candidate["handoff_id"] != old_handoff_pk
    assert db_session.query(DianxiaomiHandoff).filter_by(
        batch_id=f"selection-{candidate['selection_candidate_id']}", product_id=product_pk
    ).count() == 1
    selection_handoff = db_session.get(DianxiaomiHandoff, candidate["handoff_id"])
    assert selection_handoff.batch_id == f"selection-{candidate['selection_candidate_id']}"
    assert selection_handoff.target_url == "https://www.aliexpress.com/item/100500997.html"
    assert db_session.get(DianxiaomiHandoff, old_handoff_pk).batch_id == "manual-existing-batch"


def test_selection_intake_accepts_aliexpress_us_hosts(client, service_tokens):
    response = client.post(
        "/api/integrations/selection/candidates",
        headers=selection_headers(),
        json={
            "batch_id": "us-workbench-batch",
            "candidates": [
                {
                    "aliexpress_product_id": "100500998",
                    "url": "https://www.aliexpress.us/item/100500998.html",
                    "title": "US station candidate",
                }
            ],
        },
    )
    assert response.status_code == 201
    assert response.json()["items"][0]["url"] == "https://www.aliexpress.com/item/100500998.html"


def test_monitor_console_exposes_selection_qualification_page(client, db_session, service_tokens):
    imported = import_one(client, product_id="100501000")
    product_id = imported.json()["items"][0]["product_id"]
    record_daily_values(db_session, product_id, [0, 1, 2, 0, 1, 1, 1])
    client.put(
        "/api/selection-qualification/rule",
        json={"minimum_7d_increment_total": 6, "minimum_positive_days": 5},
    )

    response = client.get("/selection-qualification")

    assert response.status_code == 200
    assert "选品候选资格" in response.text
    assert "七日累计增量" in response.text
    assert "100501000" in response.text
    assert "QUALIFIED" in response.text
    assert "加入今日候选采集队列" in response.text


def test_daily_browser_run_queues_candidates_separately_from_competitor_store_products(
    client, db_session, service_tokens
):
    competitor_store = Store(
        name="Unrelated competitor store",
        url="https://www.aliexpress.com/store/556680",
    )
    competitor = Product(
        store=competitor_store,
        aliexpress_product_id="100501011",
        url="https://www.aliexpress.com/item/100501011.html",
        title="Competitor product",
    )
    db_session.add(competitor)
    db_session.commit()

    run_response = client.post(
        "/api/browser-collection/runs/ensure", json={"trigger": "SCHEDULED"}
    )
    assert run_response.status_code == 200
    imported = import_one(client, product_id="100501012")
    candidate_product_id = imported.json()["items"][0]["product_id"]

    product_items = db_session.query(BrowserCollectionItem).filter_by(
        run_id=run_response.json()["id"], target_type="PRODUCT"
    ).all()
    assert len(product_items) == 1
    assert product_items[0].product_id == candidate_product_id
    assert product_items[0].target_key == f"product:{candidate_product_id}:single"
    assert product_items[0].target_url == (
        "https://www.aliexpress.com/item/100501012.html#monitor_product_id=100501012"
    )
    assert product_items[0].product.monitor_scope == "selection_candidate"
    assert product_items[0].product.id != competitor.id

    queued = client.post("/api/selection-qualification/collect")
    assert queued.status_code == 200
    assert queued.json()["candidate_count"] == 1
    assert len(queued.json()["items"]) == 1


def test_active_product_is_preferred_when_duplicate_ids_exist(client, db_session, service_tokens):
    inactive_store = Store(name="Inactive duplicate", url="https://www.aliexpress.com/store/556678")
    active_store = Store(name="Active duplicate", url="https://www.aliexpress.com/store/556679")
    inactive = Product(
        store=inactive_store,
        aliexpress_product_id="100500999",
        url="https://www.aliexpress.com/item/100500999-inactive.html",
        status="inactive",
    )
    active = Product(
        store=active_store,
        aliexpress_product_id="100500999",
        url="https://www.aliexpress.com/item/100500999.html",
        status="active",
    )
    db_session.add_all([inactive, active])
    db_session.commit()
    expected_product_id = active.id

    imported = import_one(client, product_id="100500999")
    assert imported.status_code == 200
    assert imported.json()["items"][0]["product_id"] == expected_product_id


def test_selection_intake_requires_service_token_and_rejects_other_platforms(
    client, service_tokens
):
    body = {
        "batch_id": "amazon-batch",
        "candidates": [
            {
                "aliexpress_product_id": "B0AMAZON123",
                "url": "https://www.amazon.com/dp/B0AMAZON123",
            }
        ],
    }
    assert client.post("/api/integrations/selection/candidates", json=body).status_code == 401
    rejected = client.post(
        "/api/integrations/selection/candidates", headers=selection_headers(), json=body
    )
    assert rejected.status_code == 422
    mismatched_url = client.post(
        "/api/integrations/selection/candidates",
        headers=selection_headers(),
        json={
            "batch_id": "bad-url",
            "candidates": [
                {
                    "aliexpress_product_id": "100500994",
                    "url": "https://www.aliexpress.com/item/100500995.html",
                }
            ],
        },
    )
    assert mismatched_url.status_code == 422
    short_id = client.post(
        "/api/integrations/selection/candidates",
        headers=selection_headers(),
        json={
            "batch_id": "short-id",
            "candidates": [
                {"aliexpress_product_id": "12345", "url": "https://www.aliexpress.com/item/12345.html"}
            ],
        },
    )
    assert short_id.status_code == 422


def test_selection_rule_requires_user_auth_when_authentication_is_enabled(
    client, db_session, service_tokens, monkeypatch
):
    from app.core.auth import issue_session
    from app.core.config import get_settings
    from app.db.models import User

    monkeypatch.setenv("AUTH_REQUIRED", "true")
    get_settings.cache_clear()
    assert client.get("/api/selection-qualification/rule").status_code == 401

    imported = import_one(client, product_id="100500995")
    assert imported.status_code == 201
    user = User(username="rule-admin", password_hash="unused")
    db_session.add(user)
    db_session.commit()
    token, _session = issue_session(db_session, user)
    authorized = client.get(
        "/api/selection-qualification/rule",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert authorized.status_code == 200


def test_complete_seven_day_ixspy_window_qualifies_once_and_exposes_evidence(
    client, db_session, service_tokens
):
    imported = import_one(client, product_id="100500991")
    assert imported.status_code == 201
    product_id = imported.json()["items"][0]["product_id"]
    end_date = record_daily_values(db_session, product_id, [0, 1, 2, 0, 3, 4, 0])

    configured = client.put(
        "/api/selection-qualification/rule",
        json={
            "minimum_7d_increment_total": 10,
            "minimum_positive_days": 4,
        },
    )
    assert configured.status_code == 200
    candidate = next(
        item
        for item in client.get("/api/selection-qualification/candidates").json()
        if item["aliexpress_product_id"] == "100500991"
    )
    assert candidate["qualification_status"] == "QUALIFIED"
    assert candidate["evidence"]["window_start"] == (end_date - timedelta(days=6)).isoformat()
    assert candidate["evidence"]["window_end"] == end_date.isoformat()
    assert candidate["evidence"]["observed_days"] == 7
    assert candidate["evidence"]["total_increment"] == 10
    assert candidate["evidence"]["positive_days"] == 4
    assert candidate["evidence"]["daily_values"] == [0, 1, 2, 0, 3, 4, 0]
    assert candidate["handoff_id"] is not None
    assert db_session.query(DianxiaomiHandoff).filter_by(product_id=product_id).count() == 1

    client.post("/api/selection-qualification/evaluate")
    assert db_session.query(DianxiaomiHandoff).filter_by(product_id=product_id).count() == 1


def test_partial_or_non_ixspy_window_waits_without_enqueuing(client, db_session, service_tokens):
    imported = import_one(client, product_id="100500992")
    assert imported.status_code == 201
    product_id = imported.json()["items"][0]["product_id"]
    end_date = datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)
    record_daily_values(db_session, product_id, [1, 1, 1, 1, 1, 1])
    db_session.add(
        ProductDailyMetric(
            product_id=product_id,
            metric_date=end_date,
            estimated_sales=50,
            is_estimable=True,
            reason="public_cumulative_sold_delta",
            calculation_method="public_cumulative_sold_delta",
        )
    )
    db_session.commit()
    client.put(
        "/api/selection-qualification/rule",
        json={"minimum_7d_increment_total": 1, "minimum_positive_days": 1},
    )

    candidate = next(
        item
        for item in client.get("/api/selection-qualification/candidates").json()
        if item["aliexpress_product_id"] == "100500992"
    )
    assert candidate["qualification_status"] == "WAITING_FOR_DATA"
    assert candidate["evidence"]["observed_days"] == 6
    assert db_session.query(DianxiaomiHandoff).filter_by(product_id=product_id).count() == 0


def test_new_ixspy_observation_rechecks_configured_selection_candidates(
    client, db_session, service_tokens
):
    imported = import_one(client, product_id="100500996")
    assert imported.status_code == 201
    product_id = imported.json()["items"][0]["product_id"]
    client.put(
        "/api/selection-qualification/rule",
        json={"minimum_7d_increment_total": 7, "minimum_positive_days": 7},
    )
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    observed = client.post(
        "/api/collection/browser-extension",
        json={
            "platform_product_id": "100500996",
            "url": "https://www.aliexpress.com/item/100500996.html",
            "title": "Workbench candidate",
            "captured_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
            "historical_sales": [
                {
                    "date": (today - timedelta(days=offset)).isoformat(),
                    "value": 1,
                    "value_type": "daily_increment",
                }
                for offset in range(7, 0, -1)
            ],
            "raw_data": {"history_daily_capture_complete": True},
        },
    )
    assert observed.status_code == 201
    candidate = next(
        item
        for item in client.get("/api/selection-qualification/candidates").json()
        if item["aliexpress_product_id"] == "100500996"
    )
    assert candidate["qualification_status"] == "QUALIFIED"
    assert candidate["handoff_id"] is not None
    assert db_session.query(DianxiaomiHandoff).filter_by(product_id=product_id).count() == 1


def test_dianxiaomi_worker_token_claim_status_and_manual_retry_attempt_count(
    client, db_session, service_tokens
):
    imported = import_one(client, product_id="100500993")
    assert imported.status_code == 201
    product_id = imported.json()["items"][0]["product_id"]
    record_daily_values(db_session, product_id, [1, 1, 1, 1, 1, 1, 1])
    client.put(
        "/api/selection-qualification/rule",
        json={"minimum_7d_increment_total": 7, "minimum_positive_days": 7},
    )

    worker_headers = {
        **dianxiaomi_headers(),
        "X-Dianxiaomi-Worker-ID": "docker-assistant",
    }
    assert client.post(
        "/api/integrations/dianxiaomi/worker/handoffs/claim",
        headers={"X-Dianxiaomi-Worker-ID": "docker-assistant"},
        json={"worker_id": "docker-assistant"},
    ).status_code == 401
    claim = client.post(
        "/api/integrations/dianxiaomi/worker/handoffs/claim",
        headers=worker_headers,
    )
    assert claim.status_code == 200
    item = claim.json()
    assert item["handoff_id"]
    assert item["aliexpress_product_id"] == "100500993"
    assert item["url"] == "https://www.aliexpress.com/item/100500993.html"
    assert item["attempt_count"] == 0
    claimed_status = client.post(
        f"/api/integrations/dianxiaomi/worker/handoffs/{item['handoff_id']}/status",
        headers=worker_headers,
        json={"status": "CLAIMED", "detail": {"phase": "ready"}},
    )
    assert claimed_status.status_code == 200
    heartbeat = client.post(
        "/api/integrations/dianxiaomi/worker/heartbeat",
        headers=worker_headers,
        json={},
    )
    assert heartbeat.status_code == 200
    assert heartbeat.json()["worker_id"] == "docker-assistant"

    failed = client.post(
        f"/api/integrations/dianxiaomi/worker/handoffs/{item['handoff_id']}/status",
        headers=worker_headers,
        json={"status": "FAILED", "detail": {"error_type": "page_error"}},
    )
    assert failed.status_code == 200
    empty_claim = client.post(
        "/api/integrations/dianxiaomi/worker/handoffs/claim",
        headers=worker_headers,
    )
    assert empty_claim.status_code == 200
    assert empty_claim.json() is None

    retried = client.post(f"/api/integrations/dianxiaomi/handoffs/{item['handoff_id']}/resume")
    assert retried.status_code == 200
    assert retried.json()["attempt_count"] == 1

    claimed_again = client.post(
        "/api/integrations/dianxiaomi/worker/handoffs/claim",
        headers=worker_headers,
    )
    assert claimed_again.json()["attempt_count"] == 1
