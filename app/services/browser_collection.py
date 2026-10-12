from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, object_session

from app.core.config import get_settings
from app.db.models import (
    BrowserCollectionControl,
    BrowserCollectionItem,
    BrowserCollectionRun,
    BrowserCollectionWorker,
    Product,
    ProductDailyMetric,
    ProductSnapshot,
    Store,
    StorePageDiscoveryState,
)
from app.services.store_page_discovery import get_or_create_store_page_discovery_state


ITEM_TERMINAL_STATUSES = {"SUCCEEDED", "PARTIAL", "FAILED", "SKIPPED"}
RUN_ACTIVE_STATUSES = {"PENDING", "RUNNING", "NEEDS_VERIFICATION"}
LEASE_DURATION = timedelta(minutes=5)
MAX_ITEM_RUNTIME = timedelta(minutes=5)
MAX_STORE_ITEM_RUNTIME = timedelta(minutes=12)
HEARTBEAT_FRESHNESS = timedelta(minutes=2)
MANUAL_CANCEL_REASON = "manual_cancel"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _target_date(value: date | None = None) -> date:
    return value or datetime.now(ZoneInfo(get_settings().timezone)).date()


def _browser_control_row(db: Session, *, lock: bool = False) -> BrowserCollectionControl:
    statement = select(BrowserCollectionControl).where(BrowserCollectionControl.id == 1)
    if lock:
        statement = statement.with_for_update()
    control = db.scalar(statement)
    if control is None:
        control = BrowserCollectionControl(id=1)
        db.add(control)
        db.flush()
    return control


def _worker_is_online(worker: BrowserCollectionWorker | None, now: datetime) -> bool:
    heartbeat = _aware(worker.last_heartbeat_at) if worker else None
    return bool(heartbeat and now - heartbeat <= HEARTBEAT_FRESHNESS)


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


def _is_manually_cancelled(run: BrowserCollectionRun) -> bool:
    return run.status == "PAUSED" and run.paused_reason == MANUAL_CANCEL_REASON


def _products_with_recent_history_for_date(db: Session, target_date: date) -> set[int]:
    """Return products with 7 direct IXSPY dates recalculated during this run day."""
    local_tz = ZoneInfo(get_settings().timezone)
    local_start = datetime(
        target_date.year, target_date.month, target_date.day, tzinfo=local_tz
    )
    next_local_start = local_start + timedelta(days=1)
    start_utc = local_start.astimezone(timezone.utc)
    end_utc = next_local_start.astimezone(timezone.utc)
    expected_dates = {
        target_date - timedelta(days=offset)
        for offset in range(1, 8)
    }
    metrics = db.scalars(
        select(ProductDailyMetric).where(
            ProductDailyMetric.metric_date.in_(expected_dates),
            ProductDailyMetric.calculation_method == "ixspy_visible_daily_increment",
            ProductDailyMetric.is_estimable.is_(True),
            ProductDailyMetric.estimated_sales.is_not(None),
            ProductDailyMetric.calculated_at >= start_utc,
            ProductDailyMetric.calculated_at < end_utc,
        )
    )
    dates_by_product: dict[int, set[date]] = {}
    for metric in metrics:
        dates_by_product.setdefault(metric.product_id, set()).add(metric.metric_date)
    return {
        product_id
        for product_id, metric_dates in dates_by_product.items()
        if expected_dates.issubset(metric_dates)
    }


def _advance_run(db: Session, run: BrowserCollectionRun) -> None:
    db.flush()
    if run.status == "PAUSED" and run.paused_reason in {"manual_pause", MANUAL_CANCEL_REASON}:
        run.dispatch_enabled = False
        _refresh_counts(run)
        return
    if any(item.status == "NEEDS_VERIFICATION" for item in run.items):
        run.status = "NEEDS_VERIFICATION"
        run.dispatch_enabled = False
        _refresh_counts(run)
        return
    run.paused_reason = None

    for item in run.items:
        if (item.target_type == "PRODUCT" and item.target_key == f"product:{item.product_id}"
                and item.status == "PENDING"):
            item.status = "SKIPPED"
            item.completed_at = utcnow()
            item.error_type = "store_only_collection"
            item.error_message = "定时任务仅从店铺商品展示页采集"

    active_store_items = any(
        item.target_type == "STORE" and item.status not in ITEM_TERMINAL_STATUSES
        for item in run.items
    )
    if active_store_items:
        run.phase = "STORE_DISCOVERY"
    elif any(
        item.target_type == "PRODUCT" and item.target_key.endswith(":single")
        and item.status not in ITEM_TERMINAL_STATUSES
        for item in run.items
    ):
        run.phase = "PRODUCT_COLLECTION"

    active_items = [item for item in run.items if item.status not in ITEM_TERMINAL_STATUSES]
    _refresh_counts(run)
    if active_items:
        run.status = "RUNNING" if run.started_at else "PENDING"
        return

    run.phase = "COMPLETED"
    run.dispatch_enabled = False
    run.completed_at = run.completed_at or utcnow()
    if run.failed_count and run.succeeded_count == 0 and run.partial_count == 0:
        run.status = "FAILED"
    elif run.failed_count or run.partial_count:
        run.status = "PARTIAL"
    else:
        run.status = "COMPLETED"


def browser_collection_control_status(db: Session) -> dict:
    control = _browser_control_row(db)
    now = utcnow()
    workers = list(db.scalars(
        select(BrowserCollectionWorker).order_by(
            BrowserCollectionWorker.label, BrowserCollectionWorker.worker_id
        )
    ))
    worker_rows = {
        worker.worker_id: {
            "worker_id": worker.worker_id,
            "label": worker.label,
            "extension_version": worker.extension_version,
            "last_heartbeat_at": worker.last_heartbeat_at,
            "online": _worker_is_online(worker, now),
        }
        for worker in workers
    }
    selected = worker_rows.get(control.selected_worker_id)
    run = get_today_run(db)
    draining_count = 0
    if run is not None:
        draining_count = sum(
            item.status == "RUNNING"
            and item.claimed_worker_id != control.selected_worker_id
            for item in run.items
        )
    has_work = run is None or any(
        item.status in {"PENDING", "RUNNING", "NEEDS_VERIFICATION"}
        for item in run.items
    )
    return {
        "selected_worker": selected,
        "workers": list(worker_rows.values()),
        "dispatch_enabled": bool(run and run.dispatch_enabled),
        "run_status": run.status if run else None,
        "handoff_pending": draining_count > 0,
        "draining_count": draining_count,
        "can_start": bool(
            selected
            and selected["online"]
            and not draining_count
            and has_work
            and not (run and _is_manually_cancelled(run))
        ),
    }


def select_browser_collection_worker(
    db: Session, worker_id: str, *, confirm_handoff: bool = False
) -> dict:
    control = _browser_control_row(db, lock=True)
    worker = db.get(BrowserCollectionWorker, worker_id)
    if worker is None:
        raise ValueError("该采集电脑尚未连接监控台")
    run = db.scalar(
        select(BrowserCollectionRun)
        .where(BrowserCollectionRun.target_date == _target_date())
        .with_for_update()
    )
    changed = control.selected_worker_id != worker_id
    if changed and control.selected_worker_id and run is not None:
        active = run.status in RUN_ACTIVE_STATUSES or run.dispatch_enabled
        if active and not confirm_handoff:
            raise ValueError("切换采集电脑会暂停新任务领取，请确认交接")
    if changed:
        control.selected_worker_id = worker.worker_id
        control.selected_worker_label = worker.label
        if run is not None:
            run.dispatch_enabled = False
    db.commit()
    return browser_collection_control_status(db)


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
    stores = list(
        db.scalars(select(Store).where(Store.status == "active").order_by(Store.id))
    )
    for position, store in enumerate(stores, start=1):
        get_or_create_store_page_discovery_state(
            db, store, first_orders_url=_store_collection_url(store)
        )
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


def queue_all_store_collections(
    db: Session,
) -> tuple[BrowserCollectionRun, int, int, int]:
    """Queue active monitored stores for an explicit manual collection request."""
    stores = list(
        db.scalars(select(Store).where(Store.status == "active").order_by(Store.id))
    )
    if not stores:
        raise ValueError("当前没有活跃监控店铺")

    run = get_today_run(db)
    if run is not None and run.status in {"PAUSED", "NEEDS_VERIFICATION"}:
        if not _is_manually_cancelled(run):
            raise ValueError("今日店铺队列已暂停或等待验证，请先处理后再采集全部")
    if run is None:
        run = BrowserCollectionRun(target_date=_target_date(), trigger="MANUAL")
        db.add(run)
        db.flush()

    existing = {item.target_key: item for item in run.items}
    queued_count = 0
    reused_count = 0
    for store in stores:
        get_or_create_store_page_discovery_state(
            db, store, first_orders_url=_store_collection_url(store)
        )
        key = f"store:{store.id}"
        item = existing.get(key)
        if item is None:
            db.add(BrowserCollectionItem(
                run=run,
                target_type="STORE",
                target_key=key,
                store_id=store.id,
                target_url=_store_collection_url(store),
                position=max((entry.position for entry in run.items), default=0) + 1,
            ))
            queued_count += 1
        elif item.status in {"FAILED", "SKIPPED"}:
            _reset_item(item)
            item.target_url = _store_collection_url(store)
            queued_count += 1
        else:
            reused_count += 1

    run.completed_at = None
    if _is_manually_cancelled(run):
        run.paused_reason = None
        run.status = "RUNNING" if run.started_at else "PENDING"
    elif run.status not in {"NEEDS_VERIFICATION", "PAUSED"}:
        run.status = "RUNNING" if run.started_at else "PENDING"
    _advance_run(db, run)
    db.commit()
    db.refresh(run)
    return run, queued_count, reused_count, len(stores)


def get_today_run(db: Session) -> BrowserCollectionRun | None:
    return db.scalar(
        select(BrowserCollectionRun).where(
            BrowserCollectionRun.target_date == _target_date()
        )
    )


def queue_store_collection(
    db: Session, store: Store
) -> tuple[BrowserCollectionRun, int, int, bool]:
    """Queue one store-page read without opening product detail pages."""
    get_or_create_store_page_discovery_state(
        db, store, first_orders_url=_store_collection_url(store)
    )
    run = get_today_run(db)
    if run is None:
        run = BrowserCollectionRun(target_date=_target_date(), trigger="MANUAL")
        db.add(run)
        db.flush()
    elif run.status == "PAUSED" and not _is_manually_cancelled(run):
        raise ValueError("今日队列已暂停，请先恢复后再采集店铺")

    queued_count = 0
    reused_count = 0
    discovery_queued = True
    existing = {item.target_key: item for item in run.items}
    key = f"store:{store.id}"
    item = existing.get(key)
    if item is None:
        item = BrowserCollectionItem(
            run=run,
            target_type="STORE",
            target_key=key,
            store_id=store.id,
            target_url=_store_collection_url(store),
            position=max((entry.position for entry in run.items), default=0) + 1,
        )
        db.add(item)
        queued_count = 1
    elif item.status in ITEM_TERMINAL_STATUSES:
        _reset_item(item)
        item.target_url = _store_collection_url(store)
        queued_count = 1
    else:
        reused_count = 1

    run.completed_at = None
    if _is_manually_cancelled(run):
        run.paused_reason = None
        run.status = "RUNNING" if run.started_at else "PENDING"
    elif run.status not in {"NEEDS_VERIFICATION", "PAUSED"}:
        run.status = "RUNNING" if run.started_at else "PENDING"
    run.phase = "STORE_DISCOVERY"
    _advance_run(db, run)
    db.commit()
    db.refresh(run)
    return run, queued_count, reused_count, discovery_queued


def queue_single_product_collection(
    db: Session, product: Product
) -> tuple[BrowserCollectionRun, BrowserCollectionItem, bool]:
    """Queue one explicitly selected product without reopening the day's batch."""
    run = get_today_run(db)
    if run is None:
        run = BrowserCollectionRun(target_date=_target_date(), trigger="MANUAL")
        db.add(run)
        db.flush()

    cancelled_run = _is_manually_cancelled(run)
    if run.status in {"PAUSED", "NEEDS_VERIFICATION"} and not cancelled_run:
        raise ValueError("今日采集队列处于暂停或验证状态，请先处理后再启动单商品测试")

    key = f"product:{product.id}:single"
    item = db.scalar(
        select(BrowserCollectionItem).where(
            BrowserCollectionItem.run_id == run.id,
            BrowserCollectionItem.target_key == key,
        )
    )
    if item is not None and item.status in {"PENDING", "RUNNING", "NEEDS_VERIFICATION"}:
        return run, item, False

    other_active_items = db.scalars(
        select(BrowserCollectionItem).where(
            BrowserCollectionItem.run_id == run.id,
            BrowserCollectionItem.status.not_in(ITEM_TERMINAL_STATUSES),
            BrowserCollectionItem.target_key != key,
        )
    )
    if next(other_active_items, None) is not None:
        raise ValueError("今日队列已有其他进行中的任务；为避免并发，本次单商品测试未排队")

    if item is None:
        item = BrowserCollectionItem(
            run=run,
            target_type="PRODUCT",
            target_key=key,
            store_id=product.store_id,
            product_id=product.id,
            target_url=f"{product.url.split('#', 1)[0]}#monitor_product_id={product.aliexpress_product_id}",
            position=max((entry.position for entry in run.items), default=0) + 1,
        )
        db.add(item)
    else:
        _reset_item(item)
        item.store_id = product.store_id
        item.target_url = (
            f"{product.url.split('#', 1)[0]}#monitor_product_id={product.aliexpress_product_id}"
        )

    run.completed_at = None
    run.phase = "PRODUCT_COLLECTION"
    run.status = "RUNNING" if run.started_at else "PENDING"
    run.paused_reason = None
    _advance_run(db, run)
    db.commit()
    db.refresh(run)
    db.refresh(item)
    return run, item, True


def _reset_item(item: BrowserCollectionItem) -> None:
    item.status = "PENDING"
    item.attempt_count = 0
    item.claimed_at = None
    item.lease_expires_at = None
    item.claimed_worker_id = None
    item.claim_token = None
    item.completed_at = None
    item.error_type = None
    item.error_message = None
    item.snapshot_id = None


def _release_unresponsive_items(
    run: BrowserCollectionRun,
    now: datetime,
    *,
    check_lease: bool = True,
) -> None:
    for item in run.items:
        if item.status != "RUNNING":
            continue
        claimed_at = _aware(item.claimed_at)
        lease_expires_at = _aware(item.lease_expires_at)
        max_runtime = MAX_STORE_ITEM_RUNTIME if item.target_type == "STORE" else MAX_ITEM_RUNTIME
        runtime_expired = bool(claimed_at and now - claimed_at >= max_runtime)
        lease_expired = bool(check_lease and lease_expires_at and lease_expires_at <= now)
        if not runtime_expired and not lease_expired:
            continue
        if _is_manually_cancelled(run):
            item.status = "SKIPPED"
            item.error_type = "queue_cancelled"
            item.error_message = "队列已取消，未继续重试此任务"
            item.completed_at = now
            item.lease_expires_at = None
            item.claimed_worker_id = None
            item.claim_token = None
            continue
        if item.attempt_count >= 2:
            item.status = "FAILED"
            item.error_type = "browser_item_stalled" if runtime_expired else "lease_expired"
            item.error_message = "浏览器任务两次失去响应"
            item.completed_at = now
            item.lease_expires_at = None
            item.claimed_worker_id = None
            item.claim_token = None
        else:
            item.status = "PENDING"
            item.claimed_at = None
            item.lease_expires_at = None
            item.claimed_worker_id = None
            item.claim_token = None
            item.error_type = "browser_item_stalled" if runtime_expired else "lease_expired"
            item.error_message = "浏览器页面长时间未返回，任务已自动退回队列重试"


def start_browser_collection_run(
    db: Session, run: BrowserCollectionRun | None = None
) -> BrowserCollectionRun:
    if run is None:
        run = ensure_browser_collection_run(db, trigger="MANUAL")
    control = _browser_control_row(db, lock=True)
    run = db.scalar(
        select(BrowserCollectionRun).where(BrowserCollectionRun.id == run.id).with_for_update()
    )
    if run is None:
        raise ValueError("Browser collection run not found")
    if not control.selected_worker_id:
        raise ValueError("请先选择采集电脑")
    worker = db.get(BrowserCollectionWorker, control.selected_worker_id)
    now = utcnow()
    if not _worker_is_online(worker, now):
        run.dispatch_enabled = False
        db.commit()
        raise ValueError("所选采集电脑当前离线，请先连接监控台")

    _release_unresponsive_items(run, now)
    if _is_manually_cancelled(run):
        db.commit()
        raise ValueError("此队列已取消，不能整体恢复；请从店铺列表单独启动店铺页采集")

    for item in run.items:
        if item.status == "NEEDS_VERIFICATION":
            item.status = "PENDING"
            item.claimed_at = None
            item.lease_expires_at = None
            item.claimed_worker_id = None
            item.claim_token = None
            item.completed_at = None
            item.error_type = None
            item.error_message = None

    draining = [
        item for item in run.items
        if item.status == "RUNNING" and item.claimed_worker_id != control.selected_worker_id
    ]
    if draining:
        run.dispatch_enabled = False
        db.commit()
        raise ValueError("仍有旧采集电脑的任务在运行，请等待其结束或租约过期")

    if not any(item.status in {"PENDING", "RUNNING"} for item in run.items):
        _advance_run(db, run)
        db.commit()
        raise ValueError("今日队列没有待采集任务")

    run.dispatch_enabled = True
    run.started_at = run.started_at or now
    run.completed_at = None
    run.paused_reason = None
    run.status = "RUNNING"
    _advance_run(db, run)
    db.commit()
    db.refresh(run)
    return run


def claim_next_item(
    db: Session, worker_id: str | None = None
) -> tuple[BrowserCollectionRun, BrowserCollectionItem | None]:
    run = ensure_browser_collection_run(db, trigger="CATCH_UP")
    control = _browser_control_row(db, lock=True)
    run = db.scalar(
        select(BrowserCollectionRun).where(BrowserCollectionRun.id == run.id).with_for_update()
    )
    now = utcnow()
    if not worker_id or control.selected_worker_id != worker_id:
        db.commit()
        return run, None
    selected_worker = db.get(BrowserCollectionWorker, control.selected_worker_id)
    if not _worker_is_online(selected_worker, now):
        run.dispatch_enabled = False
        db.commit()
        return run, None
    if not run.dispatch_enabled:
        db.commit()
        return run, None

    _release_unresponsive_items(run, now)
    if any(
        item.status == "RUNNING" and item.claimed_worker_id != worker_id
        for item in run.items
    ):
        db.commit()
        return run, None
    if run.status == "PAUSED" and run.paused_reason in {"manual_pause", MANUAL_CANCEL_REASON}:
        db.commit()
        return run, None
    _advance_run(db, run)
    if not run.dispatch_enabled or run.status == "NEEDS_VERIFICATION" or run.phase == "COMPLETED":
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
    item.claimed_worker_id = worker_id
    item.claim_token = secrets.token_urlsafe(32)
    item.error_type = None
    item.error_message = None
    if item.target_type == "STORE" and item.store is not None:
        get_or_create_store_page_discovery_state(
            db, item.store, first_orders_url=_store_collection_url(item.store)
        )
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
    item.claimed_worker_id = None
    item.claim_token = None
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
    if _is_manually_cancelled(item.run):
        return finish_item(
            db,
            item,
            "FAILED",
            error_type=error_type,
            error_message=error_message,
        )
    if item.attempt_count < 2:
        item.status = "PENDING"
        item.claimed_at = None
        item.lease_expires_at = None
        item.claimed_worker_id = None
        item.claim_token = None
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
    run = item.run
    if _is_manually_cancelled(run):
        item.status = "SKIPPED"
        item.error_type = "queue_cancelled"
        item.error_message = "队列已取消，未继续处理验证页面"
        item.completed_at = utcnow()
        item.lease_expires_at = None
        item.claimed_worker_id = None
        item.claim_token = None
        _refresh_counts(run)
        db.commit()
        db.refresh(run)
        return run
    item.status = "NEEDS_VERIFICATION"
    item.error_type = "platform_challenge"
    item.error_message = error_message or "AliExpress 要求人工完成验证"
    item.lease_expires_at = utcnow() + LEASE_DURATION
    run.status = "NEEDS_VERIFICATION"
    run.paused_reason = "platform_challenge"
    run.dispatch_enabled = False
    _refresh_counts(run)
    db.commit()
    db.refresh(run)
    return run


def pause_run(db: Session, run: BrowserCollectionRun) -> BrowserCollectionRun:
    if run.status not in {"PENDING", "RUNNING"}:
        raise ValueError("Only a running collection queue can be paused")
    run.status = "PAUSED"
    run.paused_reason = "manual_pause"
    run.dispatch_enabled = False
    _refresh_counts(run)
    db.commit()
    db.refresh(run)
    return run


def cancel_run(db: Session, run: BrowserCollectionRun) -> BrowserCollectionRun:
    if _is_manually_cancelled(run):
        return run
    if run.status not in {"PENDING", "RUNNING", "NEEDS_VERIFICATION", "PAUSED"}:
        raise ValueError("Only an unfinished collection queue can be cancelled")

    now = utcnow()
    for item in run.items:
        if item.status not in {"PENDING", "NEEDS_VERIFICATION"}:
            continue
        item.status = "SKIPPED"
        item.claimed_at = None
        item.lease_expires_at = None
        item.completed_at = now
        item.error_type = "queue_cancelled"
        item.error_message = "用户取消了剩余采集队列"

    run.status = "PAUSED"
    run.paused_reason = MANUAL_CANCEL_REASON
    run.dispatch_enabled = False
    _refresh_counts(run)
    db.commit()
    db.refresh(run)
    return run


def resume_run(db: Session, run: BrowserCollectionRun) -> BrowserCollectionRun:
    if _is_manually_cancelled(run):
        raise ValueError("此队列已取消，不能整体恢复；请从店铺列表单独启动店铺页采集")
    if run.status not in {"NEEDS_VERIFICATION", "PAUSED"}:
        raise ValueError("Browser collection run is not paused")
    if not any(
        item.status in {"PENDING", "RUNNING", "NEEDS_VERIFICATION"}
        for item in run.items
    ):
        run.paused_reason = None
        run.status = "RUNNING" if run.started_at else "PENDING"
        run.dispatch_enabled = False
        _advance_run(db, run)
        db.commit()
        db.refresh(run)
        return run
    return start_browser_collection_run(db, run)


def retry_item(db: Session, item: BrowserCollectionItem) -> BrowserCollectionRun:
    if _is_manually_cancelled(item.run):
        raise ValueError("此队列已取消；请从店铺列表单独启动需要采集的店铺")
    if item.status not in {"FAILED", "PARTIAL"}:
        raise ValueError("Only failed or partial items can be retried")
    if item.target_type == "STORE" and item.store is not None:
        if item.store.status != "active":
            raise ValueError("该店铺监控已停用，无法重新发现商品")
        item.target_url = _store_collection_url(item.store)
    item.status = "PENDING"
    item.attempt_count = 0
    item.claimed_at = None
    item.lease_expires_at = None
    item.claimed_worker_id = None
    item.claim_token = None
    item.completed_at = None
    item.error_type = None
    item.error_message = None
    item.snapshot_id = None
    run = item.run
    run.phase = "STORE_DISCOVERY" if item.target_type == "STORE" else "PRODUCT_COLLECTION"
    if run.status != "PAUSED":
        run.status = "RUNNING" if run.started_at else "PENDING"
    run.completed_at = None
    if run.status != "PAUSED":
        run.paused_reason = None
    _refresh_counts(run)
    db.commit()
    db.refresh(run)
    return run


def validate_browser_collection_claim(
    db: Session,
    item_id: int,
    worker_id: str | None,
    claim_token: str | None,
    *,
    lock: bool = True,
) -> BrowserCollectionItem:
    if not worker_id or not claim_token:
        raise ValueError("Browser collection write requires worker_id and claim_token")
    statement = select(BrowserCollectionItem).where(BrowserCollectionItem.id == item_id)
    if lock:
        statement = statement.with_for_update()
    item = db.scalar(statement)
    if item is None:
        raise ValueError("Browser collection item not found")
    lease_expires_at = _aware(item.lease_expires_at)
    if (
        item.status not in {"RUNNING", "NEEDS_VERIFICATION"}
        or item.claimed_worker_id != worker_id
        or not item.claim_token
        or not secrets.compare_digest(item.claim_token, claim_token)
        or lease_expires_at is None
        or lease_expires_at <= utcnow()
    ):
        raise ValueError("Browser collection claim is stale or does not belong to this worker")
    return item


def record_heartbeat(
    db: Session,
    active_claims: list[tuple[int, str]] | None = None,
    *,
    worker_id: str | None = None,
    worker_label: str | None = None,
    extension_version: str | None = None,
) -> BrowserCollectionRun:
    claimed_items = [
        (validate_browser_collection_claim(db, item_id, worker_id, claim_token), claim_token)
        for item_id, claim_token in (active_claims or [])
    ]
    run = ensure_browser_collection_run(db, trigger="CATCH_UP")
    now = utcnow()
    run.last_heartbeat_at = now
    if worker_id:
        worker = db.get(BrowserCollectionWorker, worker_id)
        if worker is None:
            worker = BrowserCollectionWorker(
                worker_id=worker_id,
                label=(worker_label or "Chrome采集器").strip() or "Chrome采集器",
                extension_version=extension_version,
                last_heartbeat_at=now,
            )
            db.add(worker)
        else:
            if worker_label and worker_label.strip():
                worker.label = worker_label.strip()
            worker.extension_version = extension_version or worker.extension_version
            worker.last_heartbeat_at = now
    _release_unresponsive_items(run, now, check_lease=False)
    for item, claim_token in claimed_items:
        if (
            item.status in {"RUNNING", "NEEDS_VERIFICATION"}
            and item.claimed_worker_id == worker_id
            and item.claim_token
            and secrets.compare_digest(item.claim_token, claim_token)
        ):
            item.lease_expires_at = now + LEASE_DURATION
    _advance_run(db, run)
    db.commit()
    db.refresh(run)
    return run


def serialize_item(item: BrowserCollectionItem, *, include_targets: bool = False) -> dict:
    title = item.product.title if item.product else item.store.name if item.store else None
    targets: list[str] = []
    discovery_page_url = None
    if include_targets and item.target_type == "STORE" and item.store_id is not None:
        session = object_session(item)
        if session is not None:
            targets = sorted(session.scalars(select(Product.aliexpress_product_id).where(
                Product.store_id == item.store_id, Product.status == "active"
            )).all())
            state = session.get(StorePageDiscoveryState, item.store_id)
            discovery_page_url = state.next_page_url if state is not None else None
    return {
        "id": item.id,
        "run_id": item.run_id,
        "target_key": item.target_key,
        "target_date": item.run.target_date,
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
        "monitored_product_ids": targets,
        "discovery_page_url": discovery_page_url,
    }


def serialize_run(run: BrowserCollectionRun) -> dict:
    now = utcnow()
    heartbeat = _aware(run.last_heartbeat_at)
    session = object_session(run)
    if session is not None:
        items = list(
            session.scalars(
                select(BrowserCollectionItem)
                .options(
                    joinedload(BrowserCollectionItem.product),
                    joinedload(BrowserCollectionItem.store),
                )
                .where(BrowserCollectionItem.run_id == run.id)
                .order_by(BrowserCollectionItem.position, BrowserCollectionItem.id)
            )
        )
    else:
        items = sorted(run.items, key=lambda value: (value.position, value.id))
    current = next(
        (
            item
            for item in items
            if item.status in {"RUNNING", "NEEDS_VERIFICATION"}
        ),
        None,
    )
    terminal_count = sum(item.status in ITEM_TERMINAL_STATUSES for item in items)
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
        "dispatch_enabled": run.dispatch_enabled,
        "current_item": serialize_item(current) if current else None,
        "items": [serialize_item(item) for item in items],
    }
