import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app.collectors.http import HTTPCollector
from app.collectors.store_dependencies import build_store_discovery_collector
from app.core.config import Settings, get_settings
from app.db.session import get_session_factory
from app.services.collection_runs import run_collection_cycle
from app.services.store_discovery import discover_store_products
from app.db.models import Store

logger = logging.getLogger(__name__)
DAILY_COLLECTION_JOB_ID = "daily_public_product_snapshot"


def run_scheduled_collection() -> None:
    collector = HTTPCollector()
    store_collector = build_store_discovery_collector()
    session = get_session_factory()()
    try:
        for store in session.scalars(select(Store).where(Store.status == "active")):
            discover_store_products(session, store, store_collector, limit=20)
        summary = run_collection_cycle(session, collector)
        logger.info("Scheduled collection completed: %s", summary.to_dict())
    except Exception:
        session.rollback()
        logger.exception("Scheduled collection failed")
    finally:
        collector.close()
        store_collector.close()
        session.close()


def create_scheduler(settings: Settings | None = None) -> BackgroundScheduler:
    settings = settings or get_settings()
    scheduler = BackgroundScheduler(timezone=settings.timezone)
    scheduler.add_job(
        run_scheduled_collection,
        trigger=CronTrigger(
            hour=settings.scheduler_hour,
            minute=settings.scheduler_minute,
            timezone=settings.timezone,
        ),
        id=DAILY_COLLECTION_JOB_ID,
        name="Daily public AliExpress product snapshots",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    return scheduler
