from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import DianxiaomiHandoff, Product, Store


ACTIVE_HANDOFF_STATUSES = {
    "QUEUED",
    "CLAIMED",
    "OPENED",
    "FILLED",
    "COLLECTING",
    "NEEDS_CONFIRMATION",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_handoff_batch(db: Session, product_ids: list[int]) -> tuple[str, list[DianxiaomiHandoff]]:
    unique_ids = list(dict.fromkeys(product_ids))
    products = list(
        db.scalars(
            select(Product)
            .join(Store, Store.id == Product.store_id)
            .where(
                Product.id.in_(unique_ids),
                Product.status == "active",
                Store.status == "active",
            )
        )
    )
    by_id = {product.id: product for product in products}
    missing = [product_id for product_id in unique_ids if product_id not in by_id]
    if missing:
        raise ValueError(f"存在不可用的监控商品: {', '.join(map(str, missing))}")

    batch_id = uuid4().hex
    existing = {
        handoff.product_id
        for handoff in db.scalars(
            select(DianxiaomiHandoff).where(
                DianxiaomiHandoff.product_id.in_(unique_ids),
                DianxiaomiHandoff.status.in_(ACTIVE_HANDOFF_STATUSES),
            )
        )
    }
    rows: list[DianxiaomiHandoff] = []
    for product_id in unique_ids:
        if product_id in existing:
            continue
        product = by_id[product_id]
        rows.append(
            DianxiaomiHandoff(
                batch_id=batch_id,
                product_id=product.id,
                store_id=product.store_id,
                target_url=product.url,
                status="QUEUED",
            )
        )
    db.add_all(rows)
    db.commit()
    for row in rows:
        db.refresh(row)
    return batch_id, rows


def claim_next_handoff(db: Session, worker_id: str) -> DianxiaomiHandoff | None:
    rows = claim_handoff_batch(db, worker_id, limit=1)
    return rows[0] if rows else None


def claim_handoff_batch(
    db: Session, worker_id: str, *, limit: int = 20
) -> list[DianxiaomiHandoff]:
    now = _now()
    expired = list(
        db.scalars(
            select(DianxiaomiHandoff).where(
                DianxiaomiHandoff.status == "CLAIMED",
                DianxiaomiHandoff.claimed_at < now - timedelta(minutes=10),
            )
        )
    )
    for row in expired:
        row.status = "QUEUED"
        row.worker_id = None

    rows = list(
        db.scalars(
        select(DianxiaomiHandoff)
        .where(DianxiaomiHandoff.status == "QUEUED")
        .order_by(DianxiaomiHandoff.requested_at, DianxiaomiHandoff.id)
        .limit(limit)
        )
    )
    if not rows:
        db.commit()
        return []
    for row in rows:
        row.status = "CLAIMED"
        row.claimed_at = now
        row.worker_id = worker_id
    db.commit()
    for row in rows:
        db.refresh(row)
    return rows


def update_handoff(
    db: Session,
    row: DianxiaomiHandoff,
    status: str,
    *,
    error_type: str | None = None,
    error_message: str | None = None,
    worker_id: str | None = None,
) -> DianxiaomiHandoff:
    now = _now()
    row.status = status
    row.error_type = error_type
    row.error_message = error_message
    if worker_id:
        row.worker_id = worker_id
    if status == "OPENED":
        row.opened_at = row.opened_at or now
    if status in {"SUCCEEDED", "FAILED", "CANCELED"}:
        row.completed_at = now
    db.commit()
    db.refresh(row)
    return row


def serialize_handoff(row: DianxiaomiHandoff) -> dict:
    product = row.product
    return {
        "id": row.id,
        "batch_id": row.batch_id,
        "product_id": row.product_id,
        "store_id": row.store_id,
        "product_title": product.title if product else None,
        "platform_product_id": product.aliexpress_product_id if product else None,
        "store_name": row.store.name if row.store else None,
        "target_url": row.target_url,
        "status": row.status,
        "requested_at": row.requested_at,
        "claimed_at": row.claimed_at,
        "opened_at": row.opened_at,
        "completed_at": row.completed_at,
        "worker_id": row.worker_id,
        "error_type": row.error_type,
        "error_message": row.error_message,
    }
