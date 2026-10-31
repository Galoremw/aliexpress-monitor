from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.collectors.base import CollectorResult
from app.collectors.dependencies import get_collector
from app.core.config import Settings
from app.db.models import BrowserCollectionItem, BrowserCollectionRun, ProductSnapshot, Store
from app.scheduler import DAILY_COLLECTION_JOB_ID, create_scheduler, run_scheduled_collection


def test_scheduler_defaults_to_midnight_in_shanghai_timezone():
    scheduler = create_scheduler(
        Settings(database_url="sqlite://", timezone="Asia/Shanghai")
    )
    job = scheduler.get_job(DAILY_COLLECTION_JOB_ID)

    assert job is not None
    assert str(job.trigger.timezone) == "Asia/Shanghai"
    fields = {field.name: str(field) for field in job.trigger.fields}
    assert fields["hour"] == "0"
    assert fields["minute"] == "0"


class CycleCollector:
    name = "cycle_fixture"
    version = "1"

    def collect(self, target):
        failed = target.product_id.endswith("2")
        return CollectorResult(
            collected_at=datetime(2026, 9, 20, 2, tzinfo=timezone.utc),
            collector_name=self.name,
            collector_version=self.version,
            parse_status="failed" if failed else "success",
            cumulative_sold=None if failed else 10,
            raw_payload={"product_id": target.product_id},
            error_type="fixture_failure" if failed else None,
        )


def add_store_with_products(client):
    store = client.post(
        "/api/stores",
        json={"name": "Scheduled Store", "url": "https://www.aliexpress.com/store/440001"},
    ).json()
    for product_id in ("1005001", "1005002"):
        client.post(
            "/api/products",
            json={"store_id": store["id"], "aliexpress_product_id": product_id},
        )
    return store


def test_scheduler_registers_daily_job_in_shanghai_timezone():
    scheduler = create_scheduler(
        Settings(
            database_url="sqlite://",
            timezone="Asia/Shanghai",
            scheduler_hour=3,
            scheduler_minute=15,
        )
    )
    job = scheduler.get_job(DAILY_COLLECTION_JOB_ID)
    assert job is not None
    assert str(job.trigger.timezone) == "Asia/Shanghai"
    fields = {field.name: str(field) for field in job.trigger.fields}
    assert fields["hour"] == "3"
    assert fields["minute"] == "15"


def test_scheduled_collection_queues_store_pages_without_detail_snapshots(client, db_session, monkeypatch):
    import app.scheduler as scheduler_module

    store = add_store_with_products(client)
    calls = []
    class StubCollector:
        def close(self):
            pass

    class StubSummary:
        def to_dict(self):
            return {}

    monkeypatch.setattr(scheduler_module, "get_session_factory", lambda: lambda: db_session)
    monkeypatch.setattr(scheduler_module, "HTTPCollector",
                        lambda: (calls.append("http"), StubCollector())[1], raising=False)
    monkeypatch.setattr(scheduler_module, "build_store_discovery_collector",
                        lambda: (calls.append("discovery"), StubCollector())[1], raising=False)
    monkeypatch.setattr(scheduler_module, "discover_store_products",
                        lambda *_args, **_kwargs: calls.append("discover"), raising=False)
    monkeypatch.setattr(scheduler_module, "run_collection_cycle",
                        lambda *_args, **_kwargs: (calls.append("detail"), StubSummary())[1], raising=False)

    run_scheduled_collection()

    assert calls == []
    run = db_session.query(BrowserCollectionRun).one()
    items = db_session.query(BrowserCollectionItem).all()
    assert len(items) == 1
    assert items[0].run_id == run.id
    assert items[0].target_type == "STORE"
    assert items[0].store_id == store["id"]
    assert db_session.query(ProductSnapshot).count() == 0


def test_scheduled_collection_reuses_success_and_running_items_but_queues_incomplete_stores(
    client, db_session, monkeypatch
):
    import app.scheduler as scheduler_module

    stores = [
        Store(
            name=f"Early store {index}",
            url=f"https://www.aliexpress.com/store/44900{index}",
            aliexpress_store_id=f"44900{index}",
        )
        for index in range(1, 5)
    ]
    db_session.add_all(stores)
    db_session.flush()
    now = datetime.now(timezone.utc)
    run = BrowserCollectionRun(
        target_date=datetime.now(ZoneInfo("Asia/Shanghai")).date(),
        trigger="MANUAL",
        status="RUNNING",
        phase="STORE_DISCOVERY",
        started_at=now,
        dispatch_enabled=True,
    )
    db_session.add(run)
    db_session.flush()
    succeeded = BrowserCollectionItem(
        run_id=run.id,
        target_type="STORE",
        target_key=f"store:{stores[0].id}",
        store_id=stores[0].id,
        target_url=stores[0].url,
        position=1,
        status="SUCCEEDED",
        attempt_count=1,
        completed_at=now,
    )
    running = BrowserCollectionItem(
        run_id=run.id,
        target_type="STORE",
        target_key=f"store:{stores[1].id}",
        store_id=stores[1].id,
        target_url=stores[1].url,
        position=2,
        status="RUNNING",
        attempt_count=1,
        claimed_at=now,
        lease_expires_at=now + timedelta(minutes=5),
        claimed_worker_id="early-worker",
        claim_token="early-claim",
    )
    partial = BrowserCollectionItem(
        run_id=run.id,
        target_type="STORE",
        target_key=f"store:{stores[2].id}",
        store_id=stores[2].id,
        target_url=stores[2].url,
        position=3,
        status="PARTIAL",
        attempt_count=1,
        completed_at=now,
    )
    db_session.add_all([succeeded, running, partial])
    db_session.commit()
    monkeypatch.setattr(scheduler_module, "get_session_factory", lambda: lambda: db_session)

    run_scheduled_collection()

    db_session.expire_all()
    run = db_session.get(BrowserCollectionRun, run.id)
    items = db_session.query(BrowserCollectionItem).filter_by(run_id=run.id).all()
    by_store = {item.store_id: item for item in items}
    assert len(items) == 4
    assert by_store[stores[0].id].status == "SUCCEEDED"
    assert by_store[stores[0].id].attempt_count == 1
    assert by_store[stores[1].id].status == "RUNNING"
    assert by_store[stores[1].id].claim_token == "early-claim"
    assert by_store[stores[2].id].status == "PENDING"
    assert by_store[stores[2].id].attempt_count == 0
    assert by_store[stores[3].id].status == "PENDING"
    assert by_store[stores[3].id].position == 4
    assert run.dispatch_enabled is True


def test_scheduler_and_collect_all_leave_run_held(client, db_session, monkeypatch):
    import app.scheduler as scheduler_module

    add_store_with_products(client)
    monkeypatch.setattr(scheduler_module, "get_session_factory", lambda: lambda: db_session)

    run_scheduled_collection()
    scheduled = client.get("/api/browser-collection/runs/today")
    collected = client.post("/api/browser-collection/runs/collect-all")

    assert scheduled.status_code == 200
    assert scheduled.json()["dispatch_enabled"] is False
    assert collected.status_code == 200
    assert collected.json()["run"]["dispatch_enabled"] is False


def test_manual_collection_cycle_processes_all_active_products(client):
    add_store_with_products(client)
    client.app.dependency_overrides[get_collector] = lambda: CycleCollector()

    response = client.post("/api/jobs/collect-now")

    assert response.status_code == 200
    assert response.json()["processed"] == 2
    assert response.json()["succeeded"] == 1
    assert response.json()["failed"] == 1
    assert len(response.json()["snapshot_ids"]) == 2


def test_unexpected_collector_exception_becomes_failed_snapshot(client):
    add_store_with_products(client)

    class ExplodingCollector:
        name = "exploding"
        version = "1"

        def collect(self, target):
            raise RuntimeError("parser bug")

    client.app.dependency_overrides[get_collector] = lambda: ExplodingCollector()
    response = client.post("/api/jobs/collect-now")
    assert response.status_code == 200
    assert response.json()["failed"] == 2
