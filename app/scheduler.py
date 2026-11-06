import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.core.config import Settings, get_settings
from app.db.session import get_session_factory
from app.services.browser_collection import ensure_browser_collection_run
from app.services.dianxiaomi import reconcile_dianxiaomi_queue_worker

logger = logging.getLogger(__name__)
DAILY_COLLECTION_JOB_ID = "daily_public_product_snapshot"
DIANXIAOMI_WORKER_RECONCILE_JOB_ID = "dianxiaomi_worker_reconcile"


def run_scheduled_collection() -> None:
    session = get_session_factory()()
    try:
        run = ensure_browser_collection_run(session, trigger="SCHEDULED")
        logger.info("Scheduled store-page run ready: id=%s status=%s", run.id, run.status)
    except Exception:
        session.rollback()
        logger.exception("Unable to create scheduled store-page run")
    finally:
        session.close()


def reconcile_dianxiaomi_worker() -> None:
    session = get_session_factory()()
    try:
        reconcile_dianxiaomi_queue_worker(session)
    except Exception:
        session.rollback()
        logger.exception("Unable to reconcile Dianxiaomi collector availability")
    finally:
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
        name="Daily monitored AliExpress store pages",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        reconcile_dianxiaomi_worker,
        trigger=IntervalTrigger(seconds=30),
        id=DIANXIAOMI_WORKER_RECONCILE_JOB_ID,
        name="Pause and resume Dianxiaomi queue for its selected collector",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    return scheduler
