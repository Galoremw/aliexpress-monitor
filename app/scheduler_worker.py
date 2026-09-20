import logging
import signal
import threading

from app.core.config import get_settings
from app.scheduler import create_scheduler


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main() -> None:
    settings = get_settings()
    scheduler = create_scheduler(settings)
    stop_event = threading.Event()

    def stop_scheduler(*_args: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, stop_scheduler)
    signal.signal(signal.SIGINT, stop_scheduler)
    scheduler.start()
    logger.info("Scheduler started")
    stop_event.wait()
    scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped")


if __name__ == "__main__":
    main()
