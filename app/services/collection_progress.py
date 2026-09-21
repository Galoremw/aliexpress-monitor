from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Product, ProductSnapshot, Store


def _local_date(value: datetime | None, timezone: ZoneInfo) -> date | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.date()
    return value.astimezone(timezone).date()


def snapshot_collection_state(
    snapshot: ProductSnapshot | None, target_date: date, timezone: ZoneInfo
) -> tuple[str, str, str, datetime | None]:
    if snapshot is None or _local_date(snapshot.captured_at, timezone) != target_date:
        return "pending", "待采集", "pending", snapshot.captured_at if snapshot else None
    if snapshot.parse_status == "failed" or (
        snapshot.status == "FAILED" and snapshot.parse_status not in {"success", "partial"}
    ):
        return "failed", "采集失败", "failed", snapshot.captured_at
    if snapshot.status == "SUSPECT" or snapshot.parse_status == "partial":
        return "completed", "部分数据", "partial", snapshot.captured_at
    return "completed", "已采集", "success", snapshot.captured_at


def manual_completed_product_ids(db: Session, day_start: datetime) -> set[int]:
    """Return products completed through a non-auto collection channel today."""
    snapshot_product_ids = set(
        db.scalars(
            select(ProductSnapshot.product_id).where(
                ProductSnapshot.captured_at >= day_start,
                ProductSnapshot.source.in_(("CHROME_EXTENSION", "MANUAL", "API")),
                ProductSnapshot.parse_status.in_(("success", "partial")),
            )
        )
    )
    return snapshot_product_ids


def build_collection_progress(db: Session) -> dict:
    timezone = ZoneInfo(get_settings().timezone)
    target_date = datetime.now(timezone).date()
    products = list(
        db.scalars(
            select(Product)
            .join(Store, Store.id == Product.store_id)
            .where(Product.status == "active", Store.status == "active")
            .order_by(
                Product.store_id,
                Product.discovery_rank.is_(None),
                Product.discovery_rank,
                Product.id,
            )
        )
    )
    product_ids = [product.id for product in products]
    latest_by_product: dict[int, ProductSnapshot] = {}
    if product_ids:
        for snapshot in db.scalars(
            select(ProductSnapshot)
            .where(ProductSnapshot.product_id.in_(product_ids))
            .order_by(ProductSnapshot.captured_at.desc(), ProductSnapshot.id.desc())
        ):
            latest_by_product.setdefault(snapshot.product_id, snapshot)

    items = []
    for product in products:
        state, label, status_class, captured_at = snapshot_collection_state(
            latest_by_product.get(product.id), target_date, timezone
        )
        items.append(
            {
                "product_id": product.id,
                "store_id": product.store_id,
                "store_name": product.store.name,
                "platform_product_id": product.aliexpress_product_id,
                "title": product.title,
                "product_url": product.url,
                "status": state,
                "status_label": label,
                "status_class": status_class,
                "captured_at": captured_at,
            }
        )

    completed = sum(item["status"] == "completed" for item in items)
    failed = sum(item["status"] == "failed" for item in items)
    pending = len(items) - completed
    next_item = next((item for item in items if item["status"] != "completed"), None)
    return {
        "date": target_date,
        "total": len(items),
        "completed": completed,
        "pending": pending,
        "failed": failed,
        "items": items,
        "next_product": next_item,
    }
