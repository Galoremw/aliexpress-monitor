from collections.abc import Generator

from app.collectors.store_discovery import (
    FallbackStoreDiscoveryCollector,
    FirecrawlStoreDiscoveryCollector,
    HTTPStoreDiscoveryCollector,
    StoreDiscoveryCollector,
)
from app.core.config import get_settings


def build_store_discovery_collector() -> FallbackStoreDiscoveryCollector:
    settings = get_settings()
    return FallbackStoreDiscoveryCollector(
        primary=HTTPStoreDiscoveryCollector(),
        fallback=FirecrawlStoreDiscoveryCollector() if settings.firecrawl_enabled else None,
    )


def get_store_discovery_collector() -> Generator[StoreDiscoveryCollector, None, None]:
    collector = build_store_discovery_collector()
    try:
        yield collector
    finally:
        collector.close()
