from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.collectors.base import Collector
from app.core.config import get_settings
from app.db.models import Product, ProductSnapshot, Store
from app.services.metrics import calculate_product_daily_metrics, calculate_store_daily_metric
from app.services.snapshots import collect_product_snapshot


@dataclass(slots=True)
class CollectionRunSummary:
    processed: int
    succeeded: int
    failed: int
    snapshot_ids: list[int]

    def to_dict(self) -> dict:
        return asdict(self)


def run_collection_cycle(
    db: Session,
    collector: Collector,
    *,
    skip_current_date_snapshots: bool = False,
) -> CollectionRunSummary:
    valid_product_ids: set[int] = set()
    if skip_current_date_snapshots:
        local_timezone = ZoneInfo(get_settings().timezone)
        today = datetime.now(local_timezone).date()
        start = datetime.combine(today, time.min, tzinfo=local_timezone).astimezone(timezone.utc)
        end = start + timedelta(days=1)
        valid_product_ids = set(
            db.scalars(
                select(ProductSnapshot.product_id).where(
                    ProductSnapshot.captured_at >= start,
                    ProductSnapshot.captured_at < end,
                    ProductSnapshot.status == "VALID",
                )
            )
        )
    products = list(
        db.scalars(
            select(Product)
            .join(Store, Store.id == Product.store_id)
            .where(Product.status == "active", Store.status == "active")
            .order_by(Product.id)
        )
    )
    products = [product for product in products if product.id not in valid_product_ids]
    snapshots = [collect_product_snapshot(db, product, collector) for product in products]
    metric_dates_by_store: dict[int, set] = {}
    for product, snapshot in zip(products, snapshots, strict=True):
        metrics = calculate_product_daily_metrics(
            db, product.id, timezone_name=get_settings().timezone
        )
        if metrics:
            metric_dates_by_store.setdefault(product.store_id, set()).add(metrics[-1].metric_date)
    for store_id, metric_dates in metric_dates_by_store.items():
        for metric_date in metric_dates:
            calculate_store_daily_metric(db, store_id, metric_date)
    succeeded = sum(snapshot.parse_status != "failed" for snapshot in snapshots)
    return CollectionRunSummary(
        processed=len(snapshots),
        succeeded=succeeded,
        failed=len(snapshots) - succeeded,
        snapshot_ids=[snapshot.id for snapshot in snapshots],
    )
