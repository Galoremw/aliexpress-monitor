from __future__ import annotations

import re
import secrets
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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
    SelectionCandidate,
    Store,
    StorePageDiscoveryState,
    StoreUrlIdentity,
)
from app.services.store_page_discovery import get_or_create_store_page_discovery_state
from app.services.store_identity import normalize_store_orders_identity


ITEM_TERMINAL_STATUSES = {"SUCCEEDED", "PARTIAL", "FAILED", "SKIPPED"}
LEASE_DURATION = timedelta(minutes=5)
MAX_ITEM_RUNTIME = timedelta(minutes=5)
MAX_STORE_ITEM_RUNTIME = timedelta(minutes=20)
HEARTBEAT_FRESHNESS = timedelta(minutes=2)
MANUAL_CANCEL_REASON = "manual_cancel"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _target_date(value: date | None = None) -> date:
    return value or collection_window_end_date()


def collection_window_end_date(now: datetime | None = None) -> date:
    """Return the exclusive end date for the latest complete IXSPY window."""
    timezone_info = ZoneInfo(get_settings().timezone)
    current = now or datetime.now(timezone_info)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone_info)
    else:
        current = current.astimezone(timezone_info)
    if current.timetz().replace(tzinfo=None) < time(15, 0):
        return current.date() - timedelta(days=1)
    return current.date()


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


def _pool_reset_owns_browser(db: Session) -> bool:
    from app.services.store_pool_reset import pool_reset_dispatch_active

    return pool_reset_dispatch_active(db)


def _store_collection_id(store: Store) -> str | None:
    if store.aliexpress_store_id and store.aliexpress_store_id.isdigit():
        return store.aliexpress_store_id
    match = re.search(r"^/store/(\d+)(?:/|$)", urlsplit(store.url).path)
    return match.group(1) if match else None


def _store_collection_url(store: Store) -> str:
    store_id = _store_collection_id(store)
    if store_id:
        return (
            f"https://www.aliexpress.com/store/{store_id}/pages/all-items.html"
            "?sortType=bestmatch_sort&shop_sortType=orders_desc"
        )
    parsed = urlsplit(store.url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update({"sortType": "bestmatch_sort", "shop_sortType": "orders_desc"})
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def _supports_store_page_collection(store: Store) -> bool:
    return _store_collection_id(store) is not None


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


def _defer_legacy_store_identity_reviews(run: BrowserCollectionRun) -> None:
    for item in run.items:
        if (
            item.status != "NEEDS_VERIFICATION"
            or item.error_type != "store_identity_review"
        ):
            continue
        evidence = item.identity_review_evidence
        if item.identity_review_url and isinstance(evidence, dict):
            evidence = dict(evidence)
            evidence.setdefault("identity_review_decision", "pending")
            item.identity_review_evidence = evidence
            item.status = "SKIPPED"
            item.error_type = "store_identity_deferred"
        else:
            item.status = "FAILED"
            item.error_type = "store_identity_evidence_missing"
            item.error_message = "旧版店铺身份审核缺少页面或证据，已跳过本店铺；请在队列结束后检查"
        item.completed_at = item.completed_at or utcnow()
        item.claimed_at = None
        item.lease_expires_at = None
        item.claimed_worker_id = None
        item.claim_token = None


def _advance_run(db: Session, run: BrowserCollectionRun) -> None:
    db.flush()
    _defer_legacy_store_identity_reviews(run)
    if run.status == "PAUSED" and run.paused_reason:
        run.dispatch_enabled = False
        _refresh_counts(run)
        return
    if any(
        item.status == "NEEDS_VERIFICATION" and item.error_type == "platform_challenge"
        for item in run.items
    ):
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
    run = get_today_run(db)
    active_claim_counts: dict[str, int] = {}
    if run is not None:
        for item in run.items:
            lease_expires_at = _aware(item.lease_expires_at)
            if (
                item.status in {"RUNNING", "NEEDS_VERIFICATION"}
                and item.claimed_worker_id
                and lease_expires_at is not None
                and lease_expires_at > now
            ):
                active_claim_counts[item.claimed_worker_id] = (
                    active_claim_counts.get(item.claimed_worker_id, 0) + 1
                )
    worker_records = list(db.scalars(
        select(BrowserCollectionWorker).order_by(
            BrowserCollectionWorker.label, BrowserCollectionWorker.worker_id
        )
    ))
    worker_records_by_id = {worker.worker_id: worker for worker in worker_records}
    worker_rows = {
        worker.worker_id: {
            "worker_id": worker.worker_id,
            "label": worker.display_label_override or worker.label,
            "extension_version": worker.extension_version,
            "capabilities": worker.capabilities or [],
            "last_heartbeat_at": worker.last_heartbeat_at,
            "online": _worker_is_online(worker, now),
            "active_claim_count": active_claim_counts.get(worker.worker_id, 0),
        }
        for worker in worker_records
    }
    selected = worker_rows.get(control.selected_worker_id)
    selected_record = worker_records_by_id.get(control.selected_worker_id)
    if (
        selected is not None
        and selected_record is not None
        and not selected_record.display_label_override
        and control.selected_worker_label
    ):
        selected["label"] = control.selected_worker_label
    label_counts: dict[str, int] = {}
    for worker in worker_rows.values():
        label_counts[worker["label"]] = label_counts.get(worker["label"], 0) + 1
    for worker in worker_rows.values():
        worker["display_label"] = (
            worker["label"]
            if label_counts[worker["label"]] == 1
            else f"{worker['label']} · {worker['worker_id'][:8]}"
        )
    workers = sorted(
        worker_rows.values(),
        key=lambda worker: (worker["display_label"], worker["worker_id"]),
    )
    has_work = run is None or any(
        item.status in {"PENDING", "RUNNING", "NEEDS_VERIFICATION"}
        for item in run.items
    )
    if run is not None and run.primary_worker_id:
        eligible_worker_online = _run_has_online_worker(db, run, now)
    else:
        eligible_worker_online = bool(selected and selected["online"])
    return {
        "selected_worker": selected,
        "workers": workers,
        "dispatch_enabled": bool(run and run.dispatch_enabled),
        "run_status": run.status if run else None,
        "handoff_pending": False,
        "draining_count": 0,
        "can_start": bool(
            eligible_worker_online
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
    if control.selected_worker_id != worker_id:
        control.selected_worker_id = worker.worker_id
        control.selected_worker_label = worker.display_label_override or worker.label
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
        if trigger == "SCHEDULED":
            _reconcile_scheduled_store_items(db, run)
        added_candidates = _queue_selection_candidate_products(db, run)
        if added_candidates:
            _advance_run(db, run)
            db.commit()
            db.refresh(run)
        return run

    run = BrowserCollectionRun(target_date=selected_date, trigger=trigger)
    db.add(run)
    db.flush()
    stores = [
        store
        for store in db.scalars(select(Store).where(Store.status == "active").order_by(Store.id))
        if _supports_store_page_collection(store)
    ]
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
    _queue_selection_candidate_products(db, run)
    db.flush()
    _advance_run(db, run)
    db.commit()
    db.refresh(run)
    return run


def _reconcile_scheduled_store_items(db: Session, run: BrowserCollectionRun) -> None:
    """Reuse successful early collection and fill gaps at the scheduled time."""
    if _is_manually_cancelled(run):
        return

    stores = [
        store
        for store in db.scalars(select(Store).where(Store.status == "active").order_by(Store.id))
        if _supports_store_page_collection(store)
    ]
    existing = {item.target_key: item for item in run.items}
    changed = False
    next_position = max((item.position for item in run.items), default=0) + 1
    for store in stores:
        key = f"store:{store.id}"
        item = existing.get(key)
        if item is None:
            get_or_create_store_page_discovery_state(
                db, store, first_orders_url=_store_collection_url(store)
            )
            item = BrowserCollectionItem(
                run=run,
                target_type="STORE",
                target_key=key,
                store_id=store.id,
                target_url=_store_collection_url(store),
                position=next_position,
            )
            db.add(item)
            existing[key] = item
            next_position += 1
            changed = True
        elif item.status in {"PARTIAL", "FAILED", "SKIPPED"}:
            _reset_item(item)
            item.target_url = _store_collection_url(store)
            changed = True

    if not changed:
        return

    run.completed_at = None
    run.phase = "STORE_DISCOVERY"
    if run.status in {"PAUSED", "NEEDS_VERIFICATION"} or any(
        item.status == "NEEDS_VERIFICATION" for item in run.items
    ):
        _refresh_counts(run)
    else:
        _advance_run(db, run)
    db.commit()
    db.refresh(run)


def _queue_selection_candidate_products(
    db: Session, run: BrowserCollectionRun
) -> int:
    """Append active workbench candidates to the daily product phase, once per run."""
    candidates = list(
        db.scalars(
            select(Product)
            .join(SelectionCandidate, SelectionCandidate.product_id == Product.id)
            .where(
                Product.monitor_scope == "selection_candidate",
                Product.status == "active",
            )
            .order_by(SelectionCandidate.id)
        )
    )
    if not candidates:
        return 0
    existing_keys = set(
        db.scalars(
            select(BrowserCollectionItem.target_key).where(
                BrowserCollectionItem.run_id == run.id
            )
        )
    )
    position = max((item.position for item in run.items), default=0)
    added = 0
    for product in candidates:
        key = f"product:{product.id}:single"
        if key in existing_keys:
            continue
        position += 1
        db.add(
            BrowserCollectionItem(
                run=run,
                target_type="PRODUCT",
                target_key=key,
                store_id=product.store_id,
                product_id=product.id,
                target_url=(
                    f"https://www.aliexpress.com/item/{product.aliexpress_product_id}.html"
                    f"#monitor_product_id={product.aliexpress_product_id}"
                ),
                position=position,
            )
        )
        existing_keys.add(key)
        added += 1
    if added:
        run.completed_at = None
    return added


def queue_all_store_collections(
    db: Session,
) -> tuple[BrowserCollectionRun, int, int, int]:
    """Queue active monitored stores for an explicit manual collection request."""
    stores = [
        store
        for store in db.scalars(select(Store).where(Store.status == "active").order_by(Store.id))
        if _supports_store_page_collection(store)
    ]
    if not stores:
        raise ValueError("当前没有可按店铺页采集的活跃监控店铺")

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
    db: Session, store: Store, *, allow_paused: bool = False
) -> tuple[BrowserCollectionRun, int, int, bool]:
    """Queue one store-page read without opening product detail pages."""
    if not _supports_store_page_collection(store):
        raise ValueError("自定义监控容器没有 AliExpress 店铺商品页，不能按店铺采集")
    get_or_create_store_page_discovery_state(
        db, store, first_orders_url=_store_collection_url(store)
    )
    run = get_today_run(db)
    if run is None:
        run = BrowserCollectionRun(target_date=_target_date(), trigger="MANUAL")
        db.add(run)
        db.flush()
    elif (run.status == "PAUSED" and not _is_manually_cancelled(run)
          and not allow_paused):
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
    if _is_manually_cancelled(run) and not allow_paused:
        run.paused_reason = None
        run.status = "RUNNING" if run.started_at else "PENDING"
    elif run.status not in {"NEEDS_VERIFICATION", "PAUSED"}:
        run.status = "RUNNING" if run.started_at else "PENDING"
    run.phase = "STORE_DISCOVERY"
    if run.status in {"PAUSED", "NEEDS_VERIFICATION"} or any(
        item.status == "NEEDS_VERIFICATION" for item in run.items
    ):
        _refresh_counts(run)
    else:
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
        product_url = product.url.split("#", 1)[0]
        if product.monitor_scope == "selection_candidate":
            product_url = (
                f"https://www.aliexpress.com/item/{product.aliexpress_product_id}.html"
            )
        item = BrowserCollectionItem(
            run=run,
            target_type="PRODUCT",
            target_key=key,
            store_id=product.store_id,
            product_id=product.id,
            target_url=f"{product_url}#monitor_product_id={product.aliexpress_product_id}",
            position=max((entry.position for entry in run.items), default=0) + 1,
        )
        db.add(item)
    else:
        _reset_item(item)
        item.store_id = product.store_id
        product_url = product.url.split("#", 1)[0]
        if product.monitor_scope == "selection_candidate":
            product_url = (
                f"https://www.aliexpress.com/item/{product.aliexpress_product_id}.html"
            )
        item.target_url = f"{product_url}#monitor_product_id={product.aliexpress_product_id}"

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


def _validated_run_helpers(
    db: Session, primary_worker_id: str, helper_worker_ids: list[str] | None
) -> list[BrowserCollectionWorker]:
    worker_ids = helper_worker_ids or []
    if len(worker_ids) != len(set(worker_ids)):
        raise ValueError("辅助采集电脑不能重复")
    if primary_worker_id in worker_ids:
        raise ValueError("主采集电脑不能同时作为辅助采集电脑")
    workers = [db.get(BrowserCollectionWorker, worker_id) for worker_id in worker_ids]
    if any(worker is None for worker in workers):
        raise ValueError("辅助采集电脑未登记，请刷新电脑列表后重试")
    return [worker for worker in workers if worker is not None]


def _run_has_online_worker(
    db: Session, run: BrowserCollectionRun, now: datetime
) -> bool:
    worker_ids = [run.primary_worker_id, *(worker.worker_id for worker in run.helper_workers)]
    return any(
        _worker_is_online(db.get(BrowserCollectionWorker, worker_id), now)
        for worker_id in worker_ids
        if worker_id
    )


def _prepare_run_worker_roles(
    db: Session,
    run: BrowserCollectionRun,
    selected_worker_id: str | None,
    helper_worker_ids: list[str] | None,
    now: datetime,
) -> tuple[str | None, list[BrowserCollectionWorker] | None]:
    if run.primary_worker_id is None:
        if not selected_worker_id:
            raise ValueError("请先选择主采集电脑")
        primary = db.get(BrowserCollectionWorker, selected_worker_id)
        if not _worker_is_online(primary, now):
            run.dispatch_enabled = False
            db.commit()
            raise ValueError("所选主采集电脑当前离线，请先连接监控台")
        return primary.worker_id, _validated_run_helpers(
            db, primary.worker_id, helper_worker_ids
        )

    if helper_worker_ids is not None:
        requested_helpers = _validated_run_helpers(
            db, run.primary_worker_id, helper_worker_ids
        )
        saved_ids = {worker.worker_id for worker in run.helper_workers}
        requested_ids = {worker.worker_id for worker in requested_helpers}
        if requested_ids != saved_ids:
            raise ValueError("本轮主辅采集电脑已锁定，不能更改")

    if not _run_has_online_worker(db, run, now):
        run.dispatch_enabled = False
        db.commit()
        raise ValueError("本轮主采集电脑和辅助采集电脑均离线，暂不能恢复队列")
    return None, None


def start_browser_collection_run(
    db: Session,
    run: BrowserCollectionRun | None = None,
    helper_worker_ids: list[str] | None = None,
) -> BrowserCollectionRun:
    control = _browser_control_row(db, lock=True)
    if _pool_reset_owns_browser(db):
        raise ValueError("监控池重建任务正在占用采集电脑")
    if run is None:
        run = ensure_browser_collection_run(db, trigger="MANUAL")
    run = db.scalar(
        select(BrowserCollectionRun).where(BrowserCollectionRun.id == run.id).with_for_update()
    )
    if run is None:
        raise ValueError("Browser collection run not found")
    now = utcnow()
    primary_worker_id, helper_workers = _prepare_run_worker_roles(
        db,
        run,
        control.selected_worker_id,
        helper_worker_ids,
        now,
    )

    _release_unresponsive_items(run, now)
    if _is_manually_cancelled(run):
        db.commit()
        raise ValueError("此队列已取消，不能整体恢复；请从店铺列表单独启动店铺页采集")

    _defer_legacy_store_identity_reviews(run)

    for item in run.items:
        if item.status == "NEEDS_VERIFICATION":
            original_worker = db.get(BrowserCollectionWorker, item.claimed_worker_id)
            original_worker_online = (
                bool(item.claim_token) and _worker_is_online(original_worker, now)
            )
            if original_worker_online:
                item.status = "RUNNING"
                item.claimed_at = now
                item.lease_expires_at = now + LEASE_DURATION
            else:
                item.status = "PENDING"
                item.claimed_at = None
                item.lease_expires_at = None
                item.claimed_worker_id = None
                item.claim_token = None
                item.completed_at = None
            item.error_type = None
            item.error_message = None

    if not any(item.status in {"PENDING", "RUNNING"} for item in run.items):
        _advance_run(db, run)
        db.commit()
        raise ValueError("今日队列没有待采集任务")

    if primary_worker_id is not None:
        run.primary_worker_id = primary_worker_id
        run.helper_workers = helper_workers or []
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
) -> tuple[BrowserCollectionRun | None, BrowserCollectionItem | None]:
    control = _browser_control_row(db, lock=True)
    if _pool_reset_owns_browser(db):
        run = get_today_run(db)
        db.commit()
        return run, None
    run = ensure_browser_collection_run(db, trigger="CATCH_UP")
    run = db.scalar(
        select(BrowserCollectionRun).where(BrowserCollectionRun.id == run.id).with_for_update()
    )
    now = utcnow()
    if not worker_id:
        db.commit()
        return run, None
    worker = db.get(BrowserCollectionWorker, worker_id)
    if not _worker_is_online(worker, now):
        db.commit()
        return run, None
    if not run.dispatch_enabled:
        db.commit()
        return run, None
    if run.primary_worker_id is None or (
        worker_id != run.primary_worker_id
        and worker_id not in {helper.worker_id for helper in run.helper_workers}
    ):
        db.commit()
        return run, None

    _release_unresponsive_items(run, now)
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
    retryable = item.target_type != "STORE" or error_type == "page_not_ready"
    if retryable and item.attempt_count < 2:
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
    run = item.run
    should_pause_for_failure = (
        item.target_type != "STORE"
        and run.dispatch_enabled
        and run.paused_reason is None
        and any(
            candidate.id != item.id
            and candidate.status in {"PENDING", "RUNNING", "NEEDS_VERIFICATION"}
            for candidate in run.items
        )
    )
    result = finish_item(
        db,
        item,
        "FAILED",
        error_type=error_type,
        error_message=error_message,
    )
    if should_pause_for_failure and result.status == "RUNNING":
        result.status = "PAUSED"
        result.paused_reason = "collection_failure"
        result.dispatch_enabled = False
        result.completed_at = None
        db.commit()
        db.refresh(result)
    return result


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


def record_store_identity_review(
    db: Session,
    item: BrowserCollectionItem,
    page_url: str,
    error_type: str,
    error_message: str,
    evidence: dict,
    *,
    worker_id: str,
    claim_token: str,
) -> BrowserCollectionRun:
    if error_type not in {"store_identity_mismatch", "store_identity_unverified"}:
        raise ValueError("Invalid store identity review reason")
    route_store_id, normalized_identity = normalize_store_orders_identity(page_url)
    if evidence.get("route_store_id") != route_store_id:
        raise ValueError("Store route ID does not match the observed page URL")

    _browser_control_row(db, lock=True)
    run = db.scalar(
        select(BrowserCollectionRun)
        .where(BrowserCollectionRun.id == item.run_id)
        .with_for_update()
    )
    if run is None:
        raise ValueError("Browser collection run not found")
    claimed_item = db.scalar(
        select(BrowserCollectionItem)
        .where(BrowserCollectionItem.id == item.id)
        .with_for_update()
    )
    if claimed_item is None or claimed_item.run_id != run.id:
        raise ValueError("Browser collection item not found")
    try:
        validate_browser_collection_claim(
            db, claimed_item.id, worker_id, claim_token, lock=False
        )
    except ValueError:
        db.rollback()
        raise
    manually_paused = run.status == "PAUSED" and run.paused_reason == "manual_pause"
    actively_dispatching = (
        run.status == "RUNNING"
        and run.dispatch_enabled
        and run.paused_reason is None
    )
    if (
        claimed_item.target_type != "STORE"
        or claimed_item.store_id is None
        or claimed_item.status != "RUNNING"
        or not (actively_dispatching or manually_paused)
    ):
        raise ValueError("This store task is no longer eligible for identity review")

    review_evidence = dict(evidence)
    review_evidence["identity_error_type"] = error_type
    review_evidence["identity_review_decision"] = "pending"
    claimed_item.identity_review_url = page_url
    claimed_item.identity_review_evidence = review_evidence
    claimed_item.status = "SKIPPED"
    claimed_item.error_type = "store_identity_deferred"
    claimed_item.error_message = error_message
    claimed_item.completed_at = utcnow()
    claimed_item.claimed_at = None
    claimed_item.lease_expires_at = None
    claimed_item.claimed_worker_id = None
    claimed_item.claim_token = None
    _advance_run(db, run)
    db.commit()
    db.refresh(run)
    return run


def _locked_identity_review_context(
    db: Session, item_id: int
) -> tuple[BrowserCollectionControl, BrowserCollectionRun, BrowserCollectionItem]:
    control = _browser_control_row(db, lock=True)
    item_ref = db.get(BrowserCollectionItem, item_id)
    if item_ref is None:
        raise ValueError("Browser collection item not found")
    run = db.scalar(
        select(BrowserCollectionRun)
        .where(BrowserCollectionRun.id == item_ref.run_id)
        .with_for_update()
    )
    item = db.scalar(
        select(BrowserCollectionItem)
        .where(BrowserCollectionItem.id == item_id)
        .with_for_update()
    )
    if run is None or item is None or item.run_id != run.id:
        raise ValueError("Browser collection item not found")
    return control, run, item


def _require_current_identity_review(
    db: Session, run: BrowserCollectionRun, item: BrowserCollectionItem
) -> None:
    evidence = item.identity_review_evidence
    is_deferred_review = (
        item.status == "SKIPPED"
        and item.error_type == "store_identity_deferred"
        and isinstance(evidence, dict)
        and evidence.get("identity_review_decision") == "pending"
    )
    is_legacy_review = (
        item.status == "NEEDS_VERIFICATION"
        and item.error_type == "store_identity_review"
        and isinstance(evidence, dict)
    )
    if (
        item.target_type != "STORE"
        or item.store_id is None
        or not (is_deferred_review or is_legacy_review)
        or not item.identity_review_url
    ):
        raise ValueError("This item is not a pending store identity review")


def _identity_review_route(item: BrowserCollectionItem) -> tuple[str, str]:
    if not item.identity_review_url or not isinstance(item.identity_review_evidence, dict):
        raise ValueError("Store identity review evidence is missing")
    route_store_id, normalized_identity = normalize_store_orders_identity(
        item.identity_review_url
    )
    if item.identity_review_evidence.get("route_store_id") != route_store_id:
        raise ValueError("Store route ID does not match the reviewed evidence")
    return route_store_id, normalized_identity


def confirm_store_identity_review(db: Session, item_id: int) -> BrowserCollectionRun:
    control, run, item = _locked_identity_review_context(db, item_id)
    evidence = dict(item.identity_review_evidence or {})
    decision = evidence.get("identity_review_decision")
    if decision == "confirmed":
        db.commit()
        db.refresh(run)
        return run
    if decision == "rejected":
        raise ValueError("该店铺身份已被人工判定为错误")
    _require_current_identity_review(db, run, item)
    route_store_id, normalized_identity = _identity_review_route(item)

    previously_approved = db.scalar(
        select(StoreUrlIdentity).where(StoreUrlIdentity.source_item_id == item.id)
    )
    if previously_approved is not None:
        if (
            previously_approved.store_id != item.store_id
            or previously_approved.route_store_id != route_store_id
            or previously_approved.normalized_page_identity != normalized_identity
        ):
            raise ValueError("This task was already confirmed for a different identity")
        evidence["identity_review_decision"] = "confirmed"
        item.identity_review_evidence = evidence
        _finalize_legacy_identity_review(item)
        db.commit()
        db.refresh(run)
        return run

    now = utcnow()
    by_route = db.scalar(
        select(StoreUrlIdentity).where(StoreUrlIdentity.route_store_id == route_store_id)
    )
    by_identity = db.scalar(
        select(StoreUrlIdentity).where(
            StoreUrlIdentity.normalized_page_identity == normalized_identity
        )
    )
    if by_route is not None and by_identity is not None and by_route.id != by_identity.id:
        raise ValueError("该店铺页面身份已与另一条监控记录冲突")
    existing = by_route or by_identity
    if existing is not None:
        if (
            existing.store_id != item.store_id
            or existing.route_store_id != route_store_id
            or existing.normalized_page_identity != normalized_identity
        ):
            raise ValueError("该店铺页面身份已绑定到其他监控店铺")
    else:
        db.add(
            StoreUrlIdentity(
                store_id=item.store_id,
                route_store_id=route_store_id,
                normalized_page_identity=normalized_identity,
                first_observed_url=item.identity_review_url,
                evidence=dict(item.identity_review_evidence),
                source_item_id=item.id,
                approved_at=now,
            )
        )
        try:
            db.flush()
        except IntegrityError as exc:
            db.rollback()
            raise ValueError("该店铺页面身份已被其他监控店铺确认") from exc

    evidence["identity_review_decision"] = "confirmed"
    item.identity_review_evidence = evidence
    _finalize_legacy_identity_review(item)
    db.commit()
    db.refresh(run)
    return run


def reject_store_identity_review(db: Session, item_id: int) -> BrowserCollectionRun:
    _, run, item = _locked_identity_review_context(db, item_id)
    evidence = dict(item.identity_review_evidence or {})
    decision = evidence.get("identity_review_decision")
    if decision == "rejected":
        db.commit()
        db.refresh(run)
        return run
    if decision == "confirmed":
        raise ValueError("该店铺身份已通过人工确认")
    _require_current_identity_review(db, run, item)
    evidence["identity_review_decision"] = "rejected"
    item.identity_review_evidence = evidence
    _finalize_legacy_identity_review(item)
    db.commit()
    db.refresh(run)
    return run


def _finalize_legacy_identity_review(item: BrowserCollectionItem) -> None:
    if item.status != "NEEDS_VERIFICATION" or item.error_type != "store_identity_review":
        return
    item.status = "SKIPPED"
    item.error_type = "store_identity_deferred"
    item.completed_at = item.completed_at or utcnow()
    item.claimed_at = None
    item.lease_expires_at = None
    item.claimed_worker_id = None
    item.claim_token = None


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


def resume_run(
    db: Session,
    run: BrowserCollectionRun,
    helper_worker_ids: list[str] | None = None,
) -> BrowserCollectionRun:
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
    return start_browser_collection_run(db, run, helper_worker_ids)


def retry_item(db: Session, item: BrowserCollectionItem) -> BrowserCollectionRun:
    if _is_manually_cancelled(item.run):
        raise ValueError("此队列已取消；请从店铺列表单独启动需要采集的店铺")
    identity_evidence = item.identity_review_evidence
    identity_requeue = (
        item.status == "SKIPPED"
        and item.target_type == "STORE"
        and item.error_type == "store_identity_deferred"
        and isinstance(identity_evidence, dict)
        and identity_evidence.get("identity_review_decision") == "confirmed"
    )
    if identity_requeue:
        route_store_id, normalized_identity = _identity_review_route(item)
        alias = db.scalar(
            select(StoreUrlIdentity).where(
                StoreUrlIdentity.store_id == item.store_id,
                StoreUrlIdentity.route_store_id == route_store_id,
                StoreUrlIdentity.normalized_page_identity == normalized_identity,
            )
        )
        if alias is None:
            raise ValueError("已确认记录没有匹配的本店铺批准 URL 身份，无法重新排队")
    elif item.status not in {"FAILED", "PARTIAL"}:
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
    verification_blocked = run.status == "NEEDS_VERIFICATION"
    if run.status not in {"PAUSED", "NEEDS_VERIFICATION"}:
        run.status = "RUNNING" if run.started_at else "PENDING"
    run.completed_at = None
    if run.status not in {"PAUSED", "NEEDS_VERIFICATION"}:
        run.paused_reason = None
    if verification_blocked:
        run.dispatch_enabled = False
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
    capabilities: list[str] | None = None,
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
                capabilities=capabilities or [],
                last_heartbeat_at=now,
            )
            db.add(worker)
        else:
            if worker_label and worker_label.strip():
                worker.label = worker_label.strip()
            worker.extension_version = extension_version or worker.extension_version
            worker.capabilities = list(capabilities or [])
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
    if worker_id and not claimed_items:
        unreported_claims = db.scalars(
            select(BrowserCollectionItem)
            .where(
                BrowserCollectionItem.claimed_worker_id == worker_id,
                BrowserCollectionItem.status == "RUNNING",
                BrowserCollectionItem.claim_token.is_not(None),
                BrowserCollectionItem.lease_expires_at > now,
            )
            .with_for_update()
        ).all()
        for item in unreported_claims:
            item.lease_expires_at = now + LEASE_DURATION
    _advance_run(db, run)
    db.commit()
    db.refresh(run)
    return run


def serialize_item(item: BrowserCollectionItem, *, include_targets: bool = False) -> dict:
    title = item.product.title if item.product else item.store.name if item.store else None
    targets: list[str] = []
    target_products: list[dict[str, str | None]] = []
    discovery_page_url = None
    approved_store_url_identities: list[dict] = []
    collection_start_url = item.target_url
    if item.target_type == "STORE" and item.store_id is not None:
        session = object_session(item)
        if session is not None:
            identities = list(session.scalars(
                select(StoreUrlIdentity)
                .where(StoreUrlIdentity.store_id == item.store_id)
                .order_by(StoreUrlIdentity.approved_at.desc(), StoreUrlIdentity.id.desc())
            ))
            approved_store_url_identities = [
                {
                    "route_store_id": identity.route_store_id,
                    "normalized_page_identity": identity.normalized_page_identity,
                    "evidence": identity.evidence or {},
                }
                for identity in identities
            ]
            for identity in identities:
                try:
                    route_store_id, normalized_identity = normalize_store_orders_identity(
                        identity.first_observed_url
                    )
                except ValueError:
                    continue
                if (route_store_id != identity.route_store_id
                    or normalized_identity != identity.normalized_page_identity):
                    continue
                collection_start_url = (
                    f"https://{normalized_identity}"
                    "?sortType=bestmatch_sort&shop_sortType=orders_desc"
                )
                break
    if include_targets and item.target_type == "STORE" and item.store_id is not None:
        session = object_session(item)
        if session is not None:
            products = list(session.scalars(select(Product).where(
                Product.store_id == item.store_id,
                Product.status == "active",
                Product.monitor_scope == "competitor",
            ).order_by(Product.aliexpress_product_id)))
            targets = [product.aliexpress_product_id for product in products]
            target_products = [{
                "platform_product_id": product.aliexpress_product_id,
                "title": product.title,
                "url": product.url,
            } for product in products]
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
        "identity_review_url": item.identity_review_url,
        "identity_review_evidence": item.identity_review_evidence,
        "identity_review_pending": bool(
            item.target_type == "STORE"
            and item.error_type in {"store_identity_deferred", "store_identity_review"}
            and isinstance(item.identity_review_evidence, dict)
            and item.identity_review_evidence.get("identity_review_decision")
            in {None, "pending"}
            and item.status in {"SKIPPED", "NEEDS_VERIFICATION"}
        ),
        "identity_review_decision": (
            item.identity_review_evidence.get("identity_review_decision")
            if isinstance(item.identity_review_evidence, dict)
            else None
        ),
        "monitored_product_ids": targets,
        "monitored_products": target_products,
        "discovery_page_url": discovery_page_url,
        "approved_store_url_identities": approved_store_url_identities,
        "collection_start_url": collection_start_url,
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
        "latest_data_date": run.target_date - timedelta(days=1),
        "primary_worker_id": run.primary_worker_id,
        "helper_worker_ids": sorted(worker.worker_id for worker in run.helper_workers),
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
