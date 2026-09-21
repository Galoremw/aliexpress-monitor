from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.collectors.store_discovery import StoreDiscoveryCollector, StoreDiscoveryResult
from app.core.config import get_settings
from app.db.models import Product, ProductDailyMetric, Store, StoreSnapshot
from app.services.metrics import calculate_product_daily_metrics, calculate_store_daily_metric
from app.services.snapshots import save_store_product_snapshot


@dataclass(slots=True)
class StoreDiscoverySummary:
    snapshot_id: int
    parse_status: str
    discovered_count: int
    added_count: int
    product_links: list[str]
    deactivated_count: int = 0
    error_type: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _failed_result(collector: StoreDiscoveryCollector, store: Store, exc: Exception) -> StoreDiscoveryResult:
    return StoreDiscoveryResult(
        collected_at=datetime.now(timezone.utc),
        collector_name=getattr(collector, "name", type(collector).__name__),
        collector_version=getattr(collector, "version", "unknown"),
        parse_status="failed",
        raw_payload={"store_url": store.url},
        error_type="collector_exception",
        error_message=str(exc),
    )


def discover_store_products(
    db: Session,
    store: Store,
    collector: StoreDiscoveryCollector,
    limit: int = 20,
) -> StoreDiscoverySummary:
    try:
        result = collector.discover(store.url)
    except Exception as exc:
        result = _failed_result(collector, store, exc)

    candidates = result.candidates[:limit]
    snapshot = StoreSnapshot(
        store_id=store.id,
        collected_at=result.collected_at,
        collector_name=result.collector_name,
        collector_version=result.collector_version,
        parse_status=result.parse_status,
        candidate_count=len(result.candidates),
        source_http_status=result.source_http_status,
        raw_payload=result.raw_payload,
        raw_content=result.raw_content,
        error_type=result.error_type,
        error_message=result.error_message,
    )
    db.add(snapshot)
    db.flush()

    existing_ids = set(
        db.scalars(
            select(Product.aliexpress_product_id).where(Product.store_id == store.id)
        )
    )
    added_count = 0
    deactivated_count = 0
    product_links: list[str] = []
    observed_products: list[tuple[Product, object, int]] = []
    for rank, candidate in enumerate(candidates, start=1):
        product_links.append(candidate.url)
        existing = db.scalar(
            select(Product).where(
                Product.store_id == store.id,
                Product.aliexpress_product_id == candidate.product_id,
            )
        )
        if existing is not None:
            existing.discovery_rank = rank
            if candidate.title and not existing.title:
                existing.title = candidate.title
            if existing.discovery_source in ("store_page", "chrome_extension_store"):
                existing.status = "active"
                existing.discovered_at = result.collected_at
            observed_products.append((existing, candidate, rank))
            continue
        if candidate.product_id in existing_ids:
            continue
        product = Product(
            store_id=store.id,
            aliexpress_product_id=candidate.product_id,
            url=candidate.url,
            title=candidate.title,
            discovery_source="store_page",
            discovery_rank=rank,
            discovered_at=result.collected_at,
        )
        db.add(product)
        db.flush()
        existing_ids.add(candidate.product_id)
        added_count += 1
        observed_products.append((product, candidate, rank))
    if result.parse_status == "success" and candidates:
        current_ids = {candidate.product_id for candidate in candidates}
        pool_products = list(
            db.scalars(
                select(Product).where(
                    Product.store_id == store.id,
                    Product.discovery_source.in_(("store_page", "chrome_extension_store")),
                    Product.status == "active",
                )
            )
        )
        for product in pool_products:
            if product.aliexpress_product_id not in current_ids:
                product.status = "inactive"
                deactivated_count += 1
    db.flush()
    metric_date = result.collected_at.astimezone(
        ZoneInfo(get_settings().timezone)
    ).date()
    for product, candidate, rank in observed_products:
        save_store_product_snapshot(
            db,
            product,
            captured_at=result.collected_at,
            sold_count=candidate.public_cumulative_sold,
            title=candidate.title,
            source="AUTO",
            collector_name=result.collector_name,
            collector_version=result.collector_version,
            raw_payload={
                "collection_mode": "store_page",
                "store_snapshot_id": snapshot.id,
                "rank": rank,
                "product_id": candidate.product_id,
                "url": candidate.url,
                "title": candidate.title,
                "public_cumulative_sold": candidate.public_cumulative_sold,
            },
            commit=False,
        )
        calculate_product_daily_metrics(
            db,
            product.id,
            timezone_name=get_settings().timezone,
            commit=False,
        )
    if observed_products:
        calculate_store_daily_metric(db, store.id, metric_date, commit=False)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        snapshot = StoreSnapshot(
            store_id=store.id,
            collected_at=result.collected_at,
            collector_name=result.collector_name,
            collector_version=result.collector_version,
            parse_status="failed",
            candidate_count=len(result.candidates),
            raw_payload=result.raw_payload,
            raw_content=result.raw_content,
            error_type="product_url_conflict",
            error_message="A discovered product URL is already assigned to another store",
        )
        db.add(snapshot)
        db.commit()
        db.refresh(snapshot)
        return StoreDiscoverySummary(
            snapshot_id=snapshot.id,
            parse_status="failed",
            discovered_count=len(result.candidates),
            added_count=0,
            deactivated_count=0,
            product_links=product_links[:20],
            error_type=snapshot.error_type,
            error_message=snapshot.error_message,
        )
    db.refresh(snapshot)
    return StoreDiscoverySummary(
        snapshot_id=snapshot.id,
        parse_status=result.parse_status,
        discovered_count=len(result.candidates),
        added_count=added_count,
        deactivated_count=deactivated_count,
        product_links=product_links[:20],
        error_type=result.error_type,
        error_message=result.error_message,
    )


def store_top_products(
    db: Session, store_id: int, metric_date: date, limit: int = 20
) -> list[tuple[ProductDailyMetric, Product]]:
    return list(
        db.execute(
            select(ProductDailyMetric, Product)
            .join(Product, Product.id == ProductDailyMetric.product_id)
            .where(
                Product.store_id == store_id,
                Product.status == "active",
                ProductDailyMetric.metric_date == metric_date,
                ProductDailyMetric.is_estimable.is_(True),
            )
            .order_by(
                ProductDailyMetric.estimated_sales.desc(),
                ProductDailyMetric.product_id,
            )
            .limit(limit)
        ).all()
    )
