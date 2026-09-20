from collections.abc import Generator

from app.collectors.base import Collector
from app.collectors.http import HTTPCollector


def get_collector() -> Generator[Collector, None, None]:
    collector = HTTPCollector()
    try:
        yield collector
    finally:
        collector.close()

