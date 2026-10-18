from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import secrets
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import BrowserStoreProduct
from app.api.utils import extract_product_id, extract_store_id, normalize_aliexpress_url
from app.core.config import get_settings
from app.db.models import (
    BrowserCollectionItem,
    BrowserCollectionRun,
    BrowserCollectionWorker,
    Product,
    Store,
    StorePoolResetBatch,
    StorePoolResetStore,
)
from app.services.historical_metrics import (
    import_historical_sales,
    looks_cumulative_like_daily_values,
)
from app.services.metrics import calculate_product_daily_metrics, calculate_store_daily_metric
from app.services.product_presentation import clean_product_title
from app.services.snapshots import save_store_product_snapshot


POOL_RESET_CAPABILITY = "store_pool_reset_v1"
POOL_RESET_CANDIDATE_LIMIT = 60
POOL_RESET_PAGE_SIZE = 50
POOL_RESET_LEASE = timedelta(minutes=5)
POOL_RESET_MAX_PAGE_ATTEMPTS = 2
POOL_RESET_ACTIVE_BATCH_STATUSES = {"PENDING", "RUNNING", "NEEDS_VERIFICATION"}
POOL_RESET_ACTIVE_TASK_STATUSES = {"RUNNING", "NEEDS_VERIFICATION"}
POOL_RESET_TERMINAL_TASK_STATUSES = {"SUCCEEDED", "FAILED", "SKIPPED", "CANCELLED"}


@dataclass(frozen=True)
class RankedPoolResetCandidate:
    product: BrowserStoreProduct
    orders_rank: int
    page_url: str


def rank_pool_reset_candidates(
    candidates: list[RankedPoolResetCandidate],
    window_start: date,
    window_end: date,
) -> tuple[list[RankedPoolResetCandidate], list[dict]]:
    if (window_end - window_start).days != 6:
        raise ValueError("Pool reset window must contain exactly seven dates")

    expected_dates = {
        window_start + timedelta(days=offset)
        for offset in range(7)
    }
    seen_product_ids: set[str] = set()
    eligible: list[tuple[RankedPoolResetCandidate, int]] = []
    diagnostics: list[dict] = []

    for candidate in candidates:
        product = candidate.product
        product_id = product.platform_product_id
        reason = None
        if product_id in seen_product_ids:
            reason = "duplicate_product_id"
        else:
            seen_product_ids.add(product_id)

        values_by_date: dict[date, int] = {}
        duplicate_date = False
        if reason is None:
            if product.history_source != "ixspy_visible_daily_increment":
                reason = "history_source_untrusted"
            elif not product.history_daily_capture_complete:
                reason = "history_capture_incomplete"
            else:
                for point in product.historical_sales:
                    if point.date not in expected_dates:
                        continue
                    if point.value_type != "daily_increment" or point.date in values_by_date:
                        duplicate_date = True
                        continue
                    values_by_date[point.date] = point.value
                if duplicate_date or set(values_by_date) != expected_dates:
                    reason = "seven_day_history_incomplete"
                elif looks_cumulative_like_daily_values(
                    [values_by_date[day] for day in sorted(expected_dates)]
                ):
                    reason = "cumulative_like_history"

        if reason is not None:
            diagnostics.append({
                "product_id": product_id,
                "orders_rank": candidate.orders_rank,
                "reason": reason,
            })
        else:
            eligible.append((candidate, sum(values_by_date.values())))

    eligible.sort(
        key=lambda row: (
            -row[1],
            row[0].orders_rank,
            row[0].product.platform_product_id,
        )
    )
    selected = eligible[:20]
    diagnostics.extend(
        {
            "product_id": candidate.product.platform_product_id,
            "orders_rank": candidate.orders_rank,
            "reason": "rank_limit_exceeded",
        }
        for candidate, _ in eligible[20:]
    )
    return [candidate for candidate, _ in selected], diagnostics


def replace_store_pool_for_store(
    db: Session,
    batch: StorePoolResetBatch,
    store_task: StorePoolResetStore,
    selected_candidates: list[RankedPoolResetCandidate],
) -> dict:
    store = db.scalar(
        select(Store)
        .where(Store.id == store_task.store_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ) if store_task.store_id is not None else None
    if store is None or store.status != "active":
        return {"skipped": True, "reason": "store_inactive_or_missing", "selected_count": 0}

    ranked, diagnostics = rank_pool_reset_candidates(
        selected_candidates, batch.window_start, batch.window_end
    )
    if len(ranked) > 20:
        raise ValueError("A store pool reset cannot select more than 20 products")

    selected_ids: set[str] = set()
    resolved: list[tuple[RankedPoolResetCandidate, str, Product | None]] = []
    for candidate in ranked:
        source = candidate.product
        product_id = source.platform_product_id
        try:
            product_url = normalize_aliexpress_url(source.url, "product")
        except Exception:
            diagnostics.append({
                "product_id": product_id,
                "orders_rank": candidate.orders_rank,
                "reason": "invalid_product_url",
            })
            continue
        if extract_product_id(product_url) != product_id:
            diagnostics.append({
                "product_id": product_id,
                "orders_rank": candidate.orders_rank,
                "reason": "product_url_id_mismatch",
            })
            continue

        id_owner = db.scalar(
            select(Product).where(
                Product.aliexpress_product_id == product_id,
                Product.store_id != store.id,
            )
        )
        if id_owner is not None:
            diagnostics.append({
                "product_id": product_id,
                "orders_rank": candidate.orders_rank,
                "reason": "product_id_owned_by_other_store",
            })
            continue

        url_owner = db.scalar(select(Product).where(Product.url == product_url))
        if url_owner is not None and url_owner.store_id != store.id:
            diagnostics.append({
                "product_id": product_id,
                "orders_rank": candidate.orders_rank,
                "reason": "product_url_owned_by_other_store",
            })
            continue
        if url_owner is not None and url_owner.aliexpress_product_id != product_id:
            diagnostics.append({
                "product_id": product_id,
                "orders_rank": candidate.orders_rank,
                "reason": "product_url_id_conflict",
            })
            continue

        product = db.scalar(
            select(Product).where(
                Product.store_id == store.id,
                Product.aliexpress_product_id == product_id,
            )
        )
        resolved.append((candidate, product_url, product))
        selected_ids.add(product_id)

    now = datetime.now(timezone.utc)
    current_products = list(
        db.scalars(select(Product).where(Product.store_id == store.id).with_for_update())
    )
    deactivated_ids: list[str] = []
    for product in current_products:
        if product.status == "active" and product.aliexpress_product_id not in selected_ids:
            product.status = "inactive"
            product.pool_reset_reactivation_eligible = False
            deactivated_ids.append(product.aliexpress_product_id)

    for candidate, product_url, product in resolved:
        source = candidate.product
        if product is None:
            product = Product(
                store_id=store.id,
                aliexpress_product_id=source.platform_product_id,
                url=product_url,
                discovery_source="chrome_extension_pool_reset",
                discovered_at=now,
            )
            db.add(product)
        product.status = "active"
        product.pool_reset_reactivation_eligible = False
        product.url = product_url
        product.title = clean_product_title(source.title) if source.title else product.title
        product.discovery_source = "chrome_extension_pool_reset"
        product.discovery_rank = candidate.orders_rank
        if product.discovered_at is None:
            product.discovered_at = now
        db.flush()
        snapshot = save_store_product_snapshot(
            db,
            product,
            captured_at=now,
            sold_count=None,
            title=source.title,
            source="CHROME_EXTENSION",
            collector_name="chrome_extension_pool_reset",
            collector_version="store-pool-reset-v1",
            raw_payload={
                "collection_mode": "monitored_store_pool_reset",
                "batch_id": batch.id,
                "orders_rank": candidate.orders_rank,
                "page_url": candidate.page_url,
                "product_id": source.platform_product_id,
                "url": product.url,
                "title": source.title,
                "main_image_url": source.image_url,
            },
            historical_sales=source.historical_sales,
            history_target_date=batch.target_date,
            history_source=source.history_source,
            commit=False,
        )
        import_historical_sales(
            db,
            product.id,
            snapshot.id,
            source.historical_sales,
            target_date=batch.target_date,
            commit=False,
        )
        calculate_product_daily_metrics(db, product.id, commit=False)

    for offset in range(1, 8):
        calculate_store_daily_metric(
            db, store.id, batch.target_date - timedelta(days=offset), commit=False
        )

    db.flush()
    return {
        "skipped": False,
        "selected_count": len(resolved),
        "selected_ids": [candidate.product.platform_product_id for candidate, _, _ in resolved],
        "deactivated_ids": sorted(deactivated_ids),
        "diagnostics": diagnostics,
    }


def _reset_target_date() -> date:
    return datetime.now(ZoneInfo(get_settings().timezone)).date()


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _browser_control_row(db: Session, *, lock: bool = False):
    from app.services.browser_collection import _browser_control_row as get_control

    return get_control(db, lock=lock)


def _reset_orders_url(store: Store) -> tuple[str | None, str | None, str | None]:
    url_store_id = extract_store_id(store.url)
    platform_store_id = store.aliexpress_store_id
    if platform_store_id and not platform_store_id.isdigit():
        platform_store_id = None
    if platform_store_id and url_store_id and platform_store_id != url_store_id:
        return platform_store_id, None, "店铺记录 ID 与店铺链接 ID 不一致"
    platform_store_id = platform_store_id or url_store_id
    if not platform_store_id:
        return None, None, "店铺缺少可验证的 AliExpress 店铺 ID"
    return (
        platform_store_id,
        f"https://www.aliexpress.com/store/{platform_store_id}/pages/all-items.html"
        "?sortType=bestmatch_sort&shop_sortType=orders_desc",
        None,
    )


def _reset_page_identity(value: str, expected_store_id: str) -> str:
    try:
        normalized = normalize_aliexpress_url(value, "store")
    except Exception as exc:
        raise ValueError("店铺页 URL 无效") from exc
    parsed = urlsplit(normalized)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if (
        extract_store_id(normalized) != expected_store_id
        or parsed.path != f"/store/{expected_store_id}/pages/all-items.html"
        or {key.lower(): item for key, item in query}.get("shop_sorttype") != "orders_desc"
    ):
        raise ValueError("店铺页不是当前目标店铺的 Orders 排序页")
    stable_query = sorted(
        (
            (key, item)
            for key, item in query
            if key.lower() not in {"spm", "scm", "algo_pvid", "algo_exp_id"}
        ),
        key=lambda pair: (pair[0].lower(), pair[0], pair[1]),
    )
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, urlencode(stable_query), ""))


def _worker_selected_and_capable(db: Session, worker_id: str, now: datetime) -> BrowserCollectionWorker:
    from app.services.browser_collection import _worker_is_online

    control = _browser_control_row(db)
    if control.selected_worker_id != worker_id:
        raise ValueError("当前采集电脑不是监控台选中的电脑")
    worker = db.get(BrowserCollectionWorker, worker_id)
    if not _worker_is_online(worker, now):
        raise ValueError("所选采集电脑当前离线，请先连接监控台")
    if POOL_RESET_CAPABILITY not in (worker.capabilities or []):
        raise ValueError("所选采集电脑的扩展版本不支持监控池重建，请先重载扩展")
    return worker


def pool_reset_dispatch_active(db: Session) -> bool:
    return db.scalar(
        select(StorePoolResetBatch.id)
        .where(
            StorePoolResetBatch.status.in_({"RUNNING", "NEEDS_VERIFICATION"}),
            StorePoolResetBatch.dispatch_enabled.is_(True),
        )
        .limit(1)
    ) is not None


def serialize_pool_reset_batch(batch: StorePoolResetBatch) -> dict:
    stores = sorted(batch.stores, key=lambda item: (item.position, item.id))
    return {
        "id": batch.id,
        "target_date": batch.target_date,
        "window_start": batch.window_start,
        "window_end": batch.window_end,
        "status": batch.status,
        "dispatch_enabled": batch.dispatch_enabled,
        "created_at": batch.created_at,
        "started_at": batch.started_at,
        "completed_at": batch.completed_at,
        "stores": [
            {
                "id": item.id,
                "store_id": item.store_id,
                "platform_store_id": item.platform_store_id,
                "store_name": item.store_name,
                "orders_url": item.orders_url,
                "position": item.position,
                "status": item.status,
                "scanned_candidate_count": item.scanned_candidate_count,
                "scanned_page_count": item.scanned_page_count,
                "next_page_url": item.next_page_url,
                "diagnostics": item.diagnostics or [],
                "error_type": item.error_type,
                "error_message": item.error_message,
            }
            for item in stores
        ],
    }


def get_latest_pool_reset_batch(db: Session) -> StorePoolResetBatch | None:
    return db.scalar(
        select(StorePoolResetBatch)
        .order_by(StorePoolResetBatch.created_at.desc(), StorePoolResetBatch.id.desc())
        .limit(1)
    )


def create_pool_reset_batch(
    db: Session, *, confirm_replace: bool
) -> StorePoolResetBatch:
    if not confirm_replace:
        raise ValueError("必须确认替换现有监控商品池")
    _browser_control_row(db, lock=True)
    existing = db.scalar(
        select(StorePoolResetBatch)
        .where(StorePoolResetBatch.status.in_(POOL_RESET_ACTIVE_BATCH_STATUSES))
        .with_for_update()
        .limit(1)
    )
    if existing is not None:
        raise ValueError("已有未完成的监控池重建批次，请先继续或取消")

    target_date = _reset_target_date()
    window_end = target_date - timedelta(days=1)
    window_start = target_date - timedelta(days=7)
    batch = StorePoolResetBatch(
        target_date=target_date,
        window_start=window_start,
        window_end=window_end,
        status="PENDING",
        dispatch_enabled=False,
    )
    db.add(batch)
    db.flush()
    stores = list(
        db.scalars(
            select(Store).where(Store.status == "active").order_by(Store.id)
        )
    )
    for position, store in enumerate(stores, start=1):
        platform_store_id, orders_url, identity_error = _reset_orders_url(store)
        task = StorePoolResetStore(
            batch=batch,
            store=store,
            platform_store_id=platform_store_id,
            store_name=store.name,
            orders_url=orders_url or store.url,
            position=position,
            status="FAILED" if identity_error else "PENDING",
            next_page_url=orders_url,
            error_type="store_identity_mismatch" if identity_error else None,
            error_message=identity_error,
            diagnostics=([{"reason": "store_identity_mismatch", "message": identity_error}]
                         if identity_error else []),
        )
        db.add(task)
    if not stores:
        db.delete(batch)
        db.flush()
        raise ValueError("当前没有启用的监控店铺")
    db.commit()
    db.refresh(batch)
    return batch


def _has_active_daily_claim(db: Session, now: datetime) -> bool:
    from app.services.browser_collection import get_today_run

    run = get_today_run(db)
    if run is None:
        return False
    active = db.scalar(
        select(BrowserCollectionItem.id)
        .where(
            BrowserCollectionItem.run_id == run.id,
            BrowserCollectionItem.status.in_(POOL_RESET_ACTIVE_TASK_STATUSES),
            BrowserCollectionItem.claim_token.is_not(None),
            (BrowserCollectionItem.lease_expires_at.is_(None))
            | (BrowserCollectionItem.lease_expires_at > now),
        )
        .limit(1)
    )
    return active is not None


def start_pool_reset_batch(db: Session, batch_id: int) -> StorePoolResetBatch:
    control = _browser_control_row(db, lock=True)
    batch = db.scalar(
        select(StorePoolResetBatch)
        .where(StorePoolResetBatch.id == batch_id)
        .with_for_update()
    )
    if batch is None:
        raise ValueError("监控池重建批次不存在")
    if batch.status != "PENDING" or batch.dispatch_enabled:
        raise ValueError("该监控池重建批次当前不能启动")
    other = db.scalar(
        select(StorePoolResetBatch.id)
        .where(
            StorePoolResetBatch.id != batch.id,
            StorePoolResetBatch.status.in_({"RUNNING", "NEEDS_VERIFICATION"}),
            StorePoolResetBatch.dispatch_enabled.is_(True),
        )
        .limit(1)
    )
    if other is not None:
        raise ValueError("另一监控池重建批次正在占用采集电脑")
    now = datetime.now(timezone.utc)
    if not control.selected_worker_id:
        raise ValueError("请先选择采集电脑")
    _worker_selected_and_capable(db, control.selected_worker_id, now)
    if _has_active_daily_claim(db, now):
        raise ValueError("每日采集任务仍在运行，请等当前店铺任务结束后再开始重建")
    pending = list(
        db.scalars(
            select(StorePoolResetStore).where(
                StorePoolResetStore.batch_id == batch.id,
                StorePoolResetStore.status == "PENDING",
            )
        )
    )
    if not pending:
        raise ValueError("该批次没有可启动的店铺任务")
    batch.status = "RUNNING"
    batch.dispatch_enabled = True
    batch.started_at = batch.started_at or now
    batch.completed_at = None
    db.commit()
    db.refresh(batch)
    return batch


def cancel_pool_reset_batch(db: Session, batch_id: int) -> StorePoolResetBatch:
    _browser_control_row(db, lock=True)
    batch = db.scalar(
        select(StorePoolResetBatch)
        .where(StorePoolResetBatch.id == batch_id)
        .with_for_update()
    )
    if batch is None:
        raise ValueError("监控池重建批次不存在")
    if batch.status not in POOL_RESET_ACTIVE_BATCH_STATUSES:
        raise ValueError("该监控池重建批次已结束")
    for task in db.scalars(
        select(StorePoolResetStore)
        .where(
            StorePoolResetStore.batch_id == batch.id,
            StorePoolResetStore.status.not_in(POOL_RESET_TERMINAL_TASK_STATUSES),
        )
        .with_for_update()
    ):
        task.status = "CANCELLED"
        task.claimed_worker_id = None
        task.claim_token = None
        task.claimed_page_url = None
        task.lease_expires_at = None
        task.completed_at = datetime.now(timezone.utc)
    batch.status = "CANCELLED"
    batch.dispatch_enabled = False
    batch.completed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(batch)
    return batch


def _page_payload(page) -> dict:
    if hasattr(page, "model_dump"):
        return page.model_dump(mode="json")
    return dict(page)


def _page_digest(page_data: dict) -> str:
    encoded = json.dumps(page_data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _clear_reset_claim(task: StorePoolResetStore) -> None:
    task.claimed_worker_id = None
    task.claim_token = None
    task.claimed_page_url = None
    task.lease_expires_at = None


def _finish_reset_batch_if_terminal(db: Session, batch: StorePoolResetBatch) -> None:
    tasks = list(
        db.scalars(
            select(StorePoolResetStore).where(StorePoolResetStore.batch_id == batch.id)
        )
    )
    statuses = {task.status for task in tasks}
    if not statuses or statuses <= POOL_RESET_TERMINAL_TASK_STATUSES:
        succeeded = sum(task.status == "SUCCEEDED" for task in tasks)
        failed = sum(task.status in {"FAILED", "SKIPPED"} for task in tasks)
        batch.status = "COMPLETED" if not failed else ("PARTIAL" if succeeded else "FAILED")
        batch.dispatch_enabled = False
        batch.completed_at = datetime.now(timezone.utc)
    elif "NEEDS_VERIFICATION" in statuses:
        batch.status = "NEEDS_VERIFICATION"
    else:
        batch.status = "RUNNING"
        batch.completed_at = None


def _reset_store_task(db: Session, task_id: int, *, lock: bool = False):
    statement = select(StorePoolResetStore).where(StorePoolResetStore.id == task_id)
    if lock:
        statement = statement.with_for_update()
    task = db.scalar(statement)
    if task is None:
        raise ValueError("监控池重建店铺任务不存在")
    batch_statement = select(StorePoolResetBatch).where(
        StorePoolResetBatch.id == task.batch_id
    )
    if lock:
        batch_statement = batch_statement.with_for_update()
    batch = db.scalar(batch_statement)
    if batch is None:
        raise ValueError("监控池重建批次不存在")
    return batch, task


def claim_pool_reset_store(
    db: Session, worker_id: str
) -> tuple[StorePoolResetBatch, StorePoolResetStore, str, int] | None:
    control = _browser_control_row(db, lock=True)
    now = datetime.now(timezone.utc)
    _worker_selected_and_capable(db, worker_id, now)
    batch = db.scalar(
        select(StorePoolResetBatch)
        .where(
            StorePoolResetBatch.status == "RUNNING",
            StorePoolResetBatch.dispatch_enabled.is_(True),
        )
        .order_by(StorePoolResetBatch.created_at, StorePoolResetBatch.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if batch is None:
        db.commit()
        return None

    tasks = list(
        db.scalars(
            select(StorePoolResetStore)
            .where(
                StorePoolResetStore.batch_id == batch.id,
                StorePoolResetStore.status.in_({"RUNNING", "NEEDS_VERIFICATION"}),
            )
            .order_by(StorePoolResetStore.position, StorePoolResetStore.id)
            .with_for_update(skip_locked=True)
        )
    )
    active = next(
        (
            task for task in tasks
            if task.status == "NEEDS_VERIFICATION"
            or (_as_utc(task.lease_expires_at) and _as_utc(task.lease_expires_at) > now)
        ),
        None,
    )
    if active is not None:
        db.commit()
        return None

    for task in tasks:
        if task.status == "RUNNING":
            if task.page_attempt_count >= POOL_RESET_MAX_PAGE_ATTEMPTS:
                task.status = "FAILED"
                task.error_type = "worker_lease_expired"
                task.error_message = "采集电脑未在租约内返回该页结果；已达到重试上限"
                task.completed_at = now
                _clear_reset_claim(task)
            else:
                task.status = "PENDING"
                task.error_type = "worker_lease_expired"
                task.error_message = "采集电脑租约过期，将从当前未完成页面重试"
                _clear_reset_claim(task)
    pending = list(
        db.scalars(
            select(StorePoolResetStore)
            .where(
                StorePoolResetStore.batch_id == batch.id,
                StorePoolResetStore.status == "PENDING",
            )
            .order_by(StorePoolResetStore.position, StorePoolResetStore.id)
            .with_for_update(skip_locked=True)
        )
    )
    task = None
    for candidate in pending:
        store = db.get(Store, candidate.store_id) if candidate.store_id else None
        if store is None or store.status != "active":
            candidate.status = "SKIPPED"
            candidate.error_type = "store_inactive_or_missing"
            candidate.error_message = "店铺已停用或不存在；原监控商品池未修改"
            candidate.diagnostics = (candidate.diagnostics or []) + [
                {"reason": "store_inactive_or_missing"}
            ]
            candidate.completed_at = now
            continue
        if not candidate.next_page_url:
            candidate.status = "FAILED"
            candidate.error_type = "missing_page_checkpoint"
            candidate.error_message = "店铺任务缺少待采集页面 URL"
            candidate.completed_at = now
            continue
        task = candidate
        break

    if task is None:
        _finish_reset_batch_if_terminal(db, batch)
        db.commit()
        return None

    try:
        target_url = _reset_page_identity(task.next_page_url, task.platform_store_id or "")
    except ValueError as error:
        task.status = "FAILED"
        task.error_type = "invalid_page_checkpoint"
        task.error_message = str(error)
        task.completed_at = now
        _finish_reset_batch_if_terminal(db, batch)
        db.commit()
        return None

    token = secrets.token_urlsafe(32)
    task.status = "RUNNING"
    task.page_attempt_count += 1
    task.claimed_worker_id = worker_id
    task.claim_token = token
    task.claimed_page_url = target_url
    task.next_page_url = target_url
    task.lease_expires_at = now + POOL_RESET_LEASE
    task.error_type = None
    task.error_message = None
    db.commit()
    db.refresh(batch)
    db.refresh(task)
    candidate_limit = min(
        POOL_RESET_PAGE_SIZE,
        POOL_RESET_CANDIDATE_LIMIT - task.scanned_candidate_count,
    )
    return batch, task, token, candidate_limit


def record_pool_reset_page(
    db: Session,
    task_id: int,
    worker_id: str,
    claim_token: str,
    expected_store_id: str,
    page,
) -> StorePoolResetBatch:
    page_data = _page_payload(page)
    digest = _page_digest(page_data)
    batch, task = _reset_store_task(db, task_id, lock=True)
    if task.last_accepted_claim_token == claim_token:
        if task.last_accepted_result_digest == digest:
            return batch
        raise ValueError("同一页的重复提交内容不一致，拒绝覆盖已确认结果")

    now = datetime.now(timezone.utc)
    if (
        batch.status not in {"RUNNING", "NEEDS_VERIFICATION"}
        or not batch.dispatch_enabled
        or task.status != "RUNNING"
        or task.claimed_worker_id != worker_id
        or task.claim_token != claim_token
        or task.lease_expires_at is None
        or _as_utc(task.lease_expires_at) <= now
    ):
        raise ValueError("监控池重建页面租约已失效或不属于当前采集电脑")
    _worker_selected_and_capable(db, worker_id, now)
    if expected_store_id != task.platform_store_id:
        raise ValueError("提交的店铺 ID 与监控池重建目标不一致")
    if not isinstance(page_data.get("candidates"), list):
        raise ValueError("页面候选商品格式无效")
    try:
        submitted_page_url = _reset_page_identity(
            page_data.get("page_url", ""), task.platform_store_id or ""
        )
        expected_page_url = _reset_page_identity(
            task.claimed_page_url or "", task.platform_store_id or ""
        )
    except ValueError as error:
        raise ValueError(f"店铺页身份验证失败：{error}") from error
    if submitted_page_url != expected_page_url:
        raise ValueError("提交页面不是本次租约指定的店铺 Orders 页")
    if page_data.get("scan_complete") is not True or page_data.get("pagination_known") is not True:
        raise ValueError("店铺页商品和分页扫描尚未完整")

    candidate_limit = min(
        POOL_RESET_PAGE_SIZE,
        POOL_RESET_CANDIDATE_LIMIT - task.scanned_candidate_count,
    )
    raw_candidates = page_data["candidates"]
    if len(raw_candidates) > candidate_limit:
        raise ValueError("本页提交候选数量超过服务端分配上限")
    has_next = page_data.get("has_next_page") is True
    next_url = page_data.get("next_page_url")
    if has_next:
        if not next_url:
            raise ValueError("页面报告存在下一页，但未提供下一页 URL")
        try:
            next_url = _reset_page_identity(next_url, task.platform_store_id or "")
        except ValueError as error:
            raise ValueError(f"下一页店铺身份验证失败：{error}") from error
        if next_url == submitted_page_url or next_url in (task.visited_page_urls or []):
            raise ValueError("店铺商品页分页 URL 重复，拒绝推进游标")
    elif next_url:
        raise ValueError("页面报告无下一页，但仍提交了下一页 URL")

    page_diagnostics = page_data.get("diagnostics") or {}
    reported_count = page_diagnostics.get("scanned_candidate_count", len(raw_candidates))
    if (
        not isinstance(reported_count, int)
        or isinstance(reported_count, bool)
        or reported_count < len(raw_candidates)
        or reported_count > candidate_limit
    ):
        raise ValueError("页面报告的扫描商品数无效")

    staged = list(task.staged_candidates or [])
    seen_ids = {
        item.get("product", {}).get("platform_product_id")
        for item in staged
        if isinstance(item, dict)
    }
    page_stage = []
    duplicate_diagnostics = []
    for index, raw_candidate in enumerate(raw_candidates, start=1):
        product = BrowserStoreProduct.model_validate(raw_candidate)
        global_rank = task.scanned_candidate_count + index
        if product.platform_product_id in seen_ids:
            duplicate_diagnostics.append({
                "product_id": product.platform_product_id,
                "orders_rank": global_rank,
                "reason": "duplicate_product_id_across_pages",
            })
            continue
        seen_ids.add(product.platform_product_id)
        page_stage.append({
            "product": product.model_dump(mode="json"),
            "orders_rank": global_rank,
            "page_url": submitted_page_url,
        })

    diagnostics = list(task.diagnostics or [])
    diagnostics.append({
        "page_url": submitted_page_url,
        "scanned_candidate_count": reported_count,
        "page_diagnostics": page_diagnostics,
    })
    diagnostics.extend(duplicate_diagnostics)
    task.staged_candidates = staged + page_stage
    task.diagnostics = diagnostics
    task.visited_page_urls = list(task.visited_page_urls or []) + [submitted_page_url]
    task.scanned_candidate_count += reported_count
    task.scanned_page_count += 1
    task.page_attempt_count = 0
    task.last_accepted_claim_token = claim_token
    task.last_accepted_result_digest = digest
    _clear_reset_claim(task)

    reached_limit = task.scanned_candidate_count >= POOL_RESET_CANDIDATE_LIMIT
    if has_next and not reached_limit:
        task.status = "PENDING"
        task.next_page_url = next_url
        task.error_type = None
        task.error_message = None
        _finish_reset_batch_if_terminal(db, batch)
        db.commit()
        db.refresh(batch)
        return batch

    candidates = [
        RankedPoolResetCandidate(
            product=BrowserStoreProduct.model_validate(item["product"]),
            orders_rank=item["orders_rank"],
            page_url=item["page_url"],
        )
        for item in task.staged_candidates
    ]
    result = replace_store_pool_for_store(db, batch, task, candidates)
    task.diagnostics = (task.diagnostics or []) + result.get("diagnostics", [])
    if result.get("skipped"):
        task.status = "SKIPPED"
        task.error_type = result.get("reason")
        task.error_message = "店铺已停用或不存在；原监控商品池未修改"
    else:
        task.status = "SUCCEEDED"
        task.error_type = None
        task.error_message = None
    task.next_page_url = None
    task.completed_at = now
    _finish_reset_batch_if_terminal(db, batch)
    db.commit()
    db.refresh(batch)
    return batch


def record_pool_reset_failure(
    db: Session,
    task_id: int,
    worker_id: str,
    claim_token: str,
    error_type: str,
    error_message: str,
) -> StorePoolResetBatch:
    batch, task = _reset_store_task(db, task_id, lock=True)
    now = datetime.now(timezone.utc)
    if (
        task.status != "RUNNING"
        or task.claimed_worker_id != worker_id
        or task.claim_token != claim_token
        or task.lease_expires_at is None
        or _as_utc(task.lease_expires_at) <= now
    ):
        raise ValueError("监控池重建失败回报租约已失效")
    _worker_selected_and_capable(db, worker_id, now)
    task.error_type = error_type
    task.error_message = error_message
    task.diagnostics = (task.diagnostics or []) + [{
        "reason": error_type,
        "message": error_message,
        "page_url": task.claimed_page_url,
    }]
    if task.page_attempt_count >= POOL_RESET_MAX_PAGE_ATTEMPTS:
        task.status = "FAILED"
        task.completed_at = now
    else:
        task.status = "PENDING"
    _clear_reset_claim(task)
    _finish_reset_batch_if_terminal(db, batch)
    db.commit()
    db.refresh(batch)
    return batch


def record_pool_reset_challenge(
    db: Session,
    task_id: int,
    worker_id: str,
    claim_token: str,
    error_message: str | None = None,
    page_url: str | None = None,
) -> StorePoolResetBatch:
    batch, task = _reset_store_task(db, task_id, lock=True)
    now = datetime.now(timezone.utc)
    if (
        task.status != "RUNNING"
        or task.claimed_worker_id != worker_id
        or task.claim_token != claim_token
        or task.lease_expires_at is None
        or _as_utc(task.lease_expires_at) <= now
    ):
        raise ValueError("监控池重建验证任务租约已失效")
    _worker_selected_and_capable(db, worker_id, now)
    if page_url and _reset_page_identity(page_url, task.platform_store_id or "") != task.claimed_page_url:
        raise ValueError("验证页面不是本次采集指定的店铺页面")
    task.status = "NEEDS_VERIFICATION"
    task.error_type = "platform_verification_required"
    task.error_message = error_message or "AliExpress 需要人工验证；验证完成后继续当前店铺页"
    task.lease_expires_at = now + timedelta(minutes=30)
    batch.status = "NEEDS_VERIFICATION"
    db.commit()
    db.refresh(batch)
    return batch


def resume_pool_reset_after_challenge(
    db: Session,
    task_id: int,
    worker_id: str,
    claim_token: str,
    page_url: str,
) -> StorePoolResetBatch:
    batch, task = _reset_store_task(db, task_id, lock=True)
    now = datetime.now(timezone.utc)
    if (
        batch.status != "NEEDS_VERIFICATION"
        or task.status != "NEEDS_VERIFICATION"
        or task.claimed_worker_id != worker_id
        or task.claim_token != claim_token
    ):
        raise ValueError("监控池重建当前没有等待该电脑验证的任务")
    _worker_selected_and_capable(db, worker_id, now)
    try:
        submitted_page_url = _reset_page_identity(page_url, task.platform_store_id or "")
    except ValueError as error:
        raise ValueError("验证后页面店铺或 Orders URL 已改变；为避免错店写入，任务未恢复") from error
    if submitted_page_url != task.claimed_page_url:
        raise ValueError("验证后页面店铺或 Orders URL 已改变；为避免错店写入，任务未恢复")
    task.status = "RUNNING"
    task.lease_expires_at = now + POOL_RESET_LEASE
    task.error_type = None
    task.error_message = None
    batch.status = "RUNNING"
    db.commit()
    db.refresh(batch)
    return batch


def record_pool_reset_heartbeat(
    db: Session,
    worker_id: str,
    claim_token: str | None = None,
    *,
    worker_label: str | None = None,
    extension_version: str | None = None,
    capabilities: list[str] | None = None,
) -> dict:
    from app.services.browser_collection import utcnow

    now = utcnow()
    control = _browser_control_row(db, lock=True)
    if control.selected_worker_id != worker_id:
        raise ValueError("当前采集电脑不是监控台选中的电脑")
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
        worker.last_heartbeat_at = now
        if worker_label:
            worker.label = worker_label
        if extension_version:
            worker.extension_version = extension_version
        if capabilities is not None:
            worker.capabilities = capabilities
    if POOL_RESET_CAPABILITY not in (worker.capabilities or []):
        raise ValueError("所选采集电脑的扩展版本不支持监控池重建，请先重载扩展")
    if claim_token:
        task = db.scalar(
            select(StorePoolResetStore)
            .where(
                StorePoolResetStore.claimed_worker_id == worker_id,
                StorePoolResetStore.claim_token == claim_token,
                StorePoolResetStore.status.in_({"RUNNING", "NEEDS_VERIFICATION"}),
            )
            .with_for_update()
        )
        if task is None:
            raise ValueError("监控池重建心跳 claim token 无效")
        task.lease_expires_at = now + (
            timedelta(minutes=30)
            if task.status == "NEEDS_VERIFICATION"
            else POOL_RESET_LEASE
        )
    db.commit()
    return {
        "worker_id": worker.worker_id,
        "selected_worker_id": control.selected_worker_id,
        "heartbeat_at": now,
    }


def retry_pool_reset_store(db: Session, task_id: int) -> StorePoolResetBatch:
    batch, task = _reset_store_task(db, task_id, lock=True)
    if task.status != "FAILED":
        raise ValueError("只有失败的店铺任务可以重试")
    other = db.scalar(
        select(StorePoolResetBatch.id)
        .where(
            StorePoolResetBatch.id != batch.id,
            StorePoolResetBatch.status.in_({"RUNNING", "NEEDS_VERIFICATION"}),
            StorePoolResetBatch.dispatch_enabled.is_(True),
        )
        .limit(1)
    )
    if other is not None:
        raise ValueError("另一监控池重建批次正在占用采集电脑")
    task.status = "PENDING"
    task.page_attempt_count = 0
    task.completed_at = None
    task.error_type = None
    task.error_message = None
    _clear_reset_claim(task)
    batch.status = "PENDING"
    batch.dispatch_enabled = False
    batch.completed_at = None
    db.commit()
    db.refresh(batch)
    return batch
