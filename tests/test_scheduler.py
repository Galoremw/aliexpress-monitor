from datetime import datetime, timezone

from app.collectors.base import CollectorResult
from app.collectors.dependencies import get_collector
from app.core.config import Settings
from app.scheduler import DAILY_COLLECTION_JOB_ID, create_scheduler


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
