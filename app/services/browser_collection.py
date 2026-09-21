from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import (
    BrowserCollectionItem,
    BrowserCollectionRun,
    Product,
    ProductSnapshot,
    Store,
)


ITEM_TERMINAL_STATUSES = {"SUCCEEDED", "PARTIAL", "FAILED", "SKIPPED"}
RUN_ACTIVE_STATUSES = {"PENDING", "RUNNING", "NEEDS_VERIFICATION"}
LEASE_DURATION = timedelta(minutes=2)
HEARTBEAT_FRESHNESS = timedelta(minutes=2)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _target_date(value: date | None = None) -> date:
    return value or datetime.now(ZoneInfo(get_settings().timezone)).date()


def _date_bounds(target_date: date) -> tuple[datetime, datetime]:
    local_timezone = ZoneInfo(get_settings().timezone)
    start = datetime.combine(target_date, time.min, tzinfo=local_timezone)
    return start.astimezone(timezone.utc), (start + timedelta(days=1)).astimezone(timezone.utc)


def _store_collection_url(store: Store) -> str:
    if store.aliexpress_store_id:
        return (
            f"https://www.aliexpress.com/store/{store.aliexpress_store_id}/pages/all-items.html"
            "?sortType=bestmatch_sort&shop_sortType=orders_desc"
        )
    parsed = urlsplit(store.url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update({"sortType": "bestmatch_sort", "shop_sortType": "orders_desc"})
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def _refresh_counts(run: BrowserCollectionRun) -> None:
    items = list(run.items)
    run.total_count = len(items)
    run.succeeded_count = sum(item.status == "SUCCEEDED" for item in items)
    run.partial_count = sum(item.status == "PARTIAL" for item in items)
    run.failed_count = sum(item.status == "FAILED" for item in items)


def _valid_product_ids_for_date(db: Session, target_date: date) -> set[int]:
    start, end = _date_bounds(target_date)
    return set(
        db.scalars(
            select(ProductSnapshot.product_id).where(
                ProductSnapshot.captured_at >= start,
                ProductSnapshot.captured_at < end,
                ProductSnapshot.status == "VALID",
            )
        )
    )


def _ensure_product_items(db: Session, run: BrowserCollectionRun) -> None:
    existing_keys = {item.target_key for item in run.items}
    valid_product_ids = _valid_product_ids_for_date(db, run.target_date)
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
    position = max((item.position for item in run.items), default=0)
    for product in products:
        target_key = f"product:{product.id}"
        if target_key in existing_keys or product.id in valid_product_ids:
            continue
        position += 1
        db.add(
            BrowserCollectionItem(
                run=run,
                target_type="PRODUCT",
                target_key=target_key,
                store_id=product.store_id,
                product_id=product.id,
                target_url=(
                    f"{product.url}#monitor_product_id={product.aliexpress_product_id}"
                ),
                position=position,
            )
        )
    run.phase = "PRODUCT_COLLECTION"
    db.flush()


def _advance_run(db: Session, run: BrowserCollectionRun) -> None:
    db.flush()
    if any(item.status == "NEEDS_VERIFICATION" for item in run.items):
        run.status = "NEEDS_VERIFICATION"
        _refresh_counts(run)
        return
    run.paused_reason = None

    active_store_items = any(
        item.target_type == "STORE" and item.status not in ITEM_TERMINAL_STATUSES
        for item in run.items
    )
    if run.phase == "STORE_DISCOVERY" and not active_store_items:
        _ensure_product_items(db, run)

    active_items = [item for item in run.items if item.status not in ITEM_TERMINAL_STATUSES]
    _refresh_counts(run)
    if active_items:
        run.status = "RUNNING" if run.started_at else "PENDING"
        return

    run.phase = "COMPLETED"
    run.completed_at = run.completed_at or utcnow()
    if run.failed_count and run.succeeded_count == 0 and run.partial_count == 0:
        run.status = "FAILED"
    elif run.failed_count or run.partial_count:
        run.status = "PARTIAL"
    else:
        run.status = "COMPLETED"


def ensure_browser_collection_run(
    db: Session,
    target_date: date | None = None,
    trigger: str = "SCHEDULED",
) -> BrowserCollectionRun:
    selected_date = _target_date(target_date)
    run = db.scalar(
        select(BrowserCollectionRun).where(BrowserCollectionRun.target_date == selected_date)
    )
    if run is not None:
        return run

    run = BrowserCollectionRun(target_date=selected_date, trigger=trigger)
    db.add(run)
    db.flush()
    stores = list(db.scalars(select(Store).where(Store.status == "active").order_by(Store.id)))
    for position, store in enumerate(stores, start=1):
        db.add(
            BrowserCollectionItem(
                run=run,
                target_type="STORE",
                target_key=f"store:{store.id}",
                store_id=store.id,
                target_url=_store_collection_url(store),
                position=position,
            )
        )
    db.flush()
    _advance_run(db, run)
    db.commit()
    db.refresh(run)
    return run


def get_today_run(db: Session) -> BrowserCollectionRun | None:
    return db.scalar(
        select(BrowserCollectionRun).where(
            BrowserCollectionRun.target_date == _target_date()
        )
    )


def claim_next_item(db: Session) -> tuple[BrowserCollectionRun, BrowserCollectionItem | None]:
    run = ensure_browser_collection_run(db, trigger="CATCH_UP")
    now = utcnow()
    for item in run.items:
        lease_expires_at = _aware(item.lease_expires_at)
        if item.status == "RUNNING" and lease_expires_at and lease_expires_at <= now:
            if item.attempt_count >= 2:
                item.status = "FAILED"
                item.error_type = "lease_expired"
                item.error_message = "浏览器任务两次失去响应"
                item.completed_at = now
            else:
                item.status = "PENDING"
                item.claimed_at = None
                item.lease_expires_at = None
    _advance_run(db, run)
    if run.status == "NEEDS_VERIFICATION" or run.phase == "COMPLETED":
        db.commit()
        return run, None

    target_type = "STORE" if run.phase == "STORE_DISCOVERY" else "PRODUCT"
    item = db.scalar(
        select(BrowserCollectionItem)
        .where(
            BrowserCollectionItem.run_id == run.id,
            BrowserCollectionItem.target_type == target_type,
            BrowserCollectionItem.status == "PENDING",
        )
        .order_by(BrowserCollectionItem.position, BrowserCollectionItem.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if item is None:
        _advance_run(db, run)
        db.commit()
        return run, None

    item.status = "RUNNING"
    item.attempt_count += 1
    item.claimed_at = now
    item.lease_expires_at = now + LEASE_DURATION
    item.error_type = None
    item.error_message = None
    run.status = "RUNNING"
    run.started_at = run.started_at or now
    run.completed_at = None
    db.commit()
    db.refresh(item)
    return run, item


def finish_item(
    db: Session,
    item: BrowserCollectionItem,
    status: str,
    *,
    snapshot_id: int | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
    commit: bool = True,
) -> BrowserCollectionRun:
    item.status = status
    item.snapshot_id = snapshot_id
    item.error_type = error_type
    item.error_message = error_message
    item.completed_at = utcnow()
    item.lease_expires_at = None
    run = item.run
    _advance_run(db, run)
    if commit:
        db.commit()
        db.refresh(run)
    else:
        db.flush()
    return run


def fail_item(
    db: Session,
    item: BrowserCollectionItem,
    error_type: str,
    error_message: str,
) -> BrowserCollectionRun:
    if item.attempt_count < 2:
        item.status = "PENDING"
        item.claimed_at = None
        item.lease_expires_at = None
        item.error_type = error_type
        item.error_message = error_message
        run = item.run
        _refresh_counts(run)
        db.commit()
        return run
    return finish_item(
        db,
        item,
        "FAILED",
        error_type=error_type,
        error_message=error_message,
    )


def pause_for_challenge(
    db: Session,
    item: BrowserCollectionItem,
    error_message: str | None = None,
) -> BrowserCollectionRun:
    item.status = "NEEDS_VERIFICATION"
    item.error_type = "platform_challenge"
    item.error_message = error_message or "AliExpress 要求人工完成验证"
    item.lease_expires_at = None
    run = item.run
    run.status = "NEEDS_VERIFICATION"
    run.paused_reason = "platform_challenge"
    _refresh_counts(run)
    db.commit()
    db.refresh(run)
    return run


def resume_run(db: Session, run: BrowserCollectionRun) -> BrowserCollectionRun:
    for item in run.items:
        if item.status == "NEEDS_VERIFICATION":
            item.status = "PENDING"
            item.claimed_at = None
            item.lease_expires_at = None
    run.status = "RUNNING"
    run.paused_reason = None
    run.completed_at = None
    db.commit()
    db.refresh(run)
    return run


def record_heartbeat(db: Session) -> BrowserCollectionRun:
    run = ensure_browser_collection_run(db, trigger="CATCH_UP")
    run.last_heartbeat_at = utcnow()
    db.commit()
    db.refresh(run)
    return run


def serialize_item(item: BrowserCollectionItem) -> dict:
    title = item.product.title if item.product else item.store.name if item.store else None
    return {
        "id": item.id,
        "run_id": item.run_id,
        "target_type": item.target_type,
        "store_id": item.store_id,
        "product_id": item.product_id,
        "title": title,
        "target_url": item.target_url,
        "position": item.position,
        "status": item.status,
        "attempt_count": item.attempt_count,
        "claimed_at": item.claimed_at,
        "completed_at": item.completed_at,
        "error_type": item.error_type,
        "error_message": item.error_message,
    }


def serialize_run(run: BrowserCollectionRun) -> dict:
    now = utcnow()
    heartbeat = _aware(run.last_heartbeat_at)
    current = next(
        (
            item
            for item in sorted(run.items, key=lambda value: (value.position, value.id))
            if item.status in {"RUNNING", "NEEDS_VERIFICATION"}
        ),
        None,
    )
    terminal_count = sum(item.status in ITEM_TERMINAL_STATUSES for item in run.items)
    return {
        "id": run.id,
        "target_date": run.target_date,
        "trigger": run.trigger,
        "status": run.status,
        "phase": run.phase,
        "total_count": run.total_count,
        "succeeded_count": run.succeeded_count,
        "partial_count": run.partial_count,
        "failed_count": run.failed_count,
        "pending_count": max(run.total_count - terminal_count, 0),
        "chrome_online": bool(heartbeat and now - heartbeat <= HEARTBEAT_FRESHNESS),
        "created_at": run.created_at,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        "last_heartbeat_at": run.last_heartbeat_at,
        "paused_reason": run.paused_reason,
        "current_item": serialize_item(current) if current else None,
    }
