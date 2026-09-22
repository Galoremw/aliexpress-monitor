from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.schemas import (
    BrowserCollectionChallengeRequest,
    BrowserCollectionClaimRead,
    BrowserCollectionFailureRequest,
    BrowserCollectionHeartbeatRequest,
    BrowserCollectionRunEnsureRequest,
    BrowserCollectionRunRead,
    BrowserExtensionCollectionRequest,
    DianxiaomiHandoffBatchRead,
    DianxiaomiHandoffClaimRequest,
    DianxiaomiHandoffCreate,
    DianxiaomiHandoffRead,
    DianxiaomiHandoffStatusRequest,
    BrowserStoreDiscoveryRead,
    BrowserStoreDiscoveryRequest,
    CollectionAttemptRead,
    CollectionProgressRead,
    CollectionPendingRead,
    CollectionStatusRead,
    ManualCollectionRequest,
    ProductCreate,
    ProductRead,
    ProductDailyMetricRead,
    ProductSnapshotRead,
    ProductUpdate,
    StoreCreate,
    StoreRead,
    StoreDailyMetricRead,
    StoreDiscoverySummaryRead,
    StoreSnapshotRead,
    StoreTopProductsRead,
    TopProductRead,
    StoreUpdate,
)
from app.collectors.base import Collector, CollectorResult
from app.collectors.dependencies import get_collector
from app.collectors.store_dependencies import get_store_discovery_collector
from app.collectors.store_discovery import StoreDiscoveryCollector
from app.core.auth import require_authenticated
from app.core.config import get_settings
from app.api.utils import (
    canonical_product_url,
    extract_product_id,
    extract_store_id,
    normalize_aliexpress_url,
)
from app.db.models import (
    BrowserCollectionItem,
    BrowserCollectionRun,
    CollectionAttempt,
    DianxiaomiHandoff,
    ManualCollectionTask,
    Product,
    ProductDailyMetric,
    ProductSnapshot,
    Store,
    StoreDailyMetric,
    StoreSnapshot,
)
from app.db.session import get_db
from app.services.snapshots import collect_product_snapshot
from app.services.snapshots import save_external_snapshot, save_store_product_snapshot
from app.services.collection_runs import run_collection_cycle
from app.services.metrics import calculate_product_daily_metrics, calculate_store_daily_metric
from app.services.historical_metrics import import_historical_sales
from app.services.store_discovery import discover_store_products, store_top_products
from app.services.collection_progress import build_collection_progress, manual_completed_product_ids
from app.services.browser_collection import (
    claim_next_item,
    ensure_browser_collection_run,
    fail_item,
    finish_item,
    get_today_run,
    pause_for_challenge,
    record_heartbeat,
    resume_run,
    serialize_item,
    serialize_run,
)
from app.services.dianxiaomi import (
    ACTIVE_HANDOFF_STATUSES,
    claim_handoff_batch,
    claim_next_handoff,
    create_handoff_batch,
    serialize_handoff,
    update_handoff,
)

router = APIRouter(prefix="/api", dependencies=[Depends(require_authenticated)])


def _handoff_or_404(db: Session, handoff_id: int) -> DianxiaomiHandoff:
    handoff = db.get(DianxiaomiHandoff, handoff_id)
    if handoff is None:
        raise HTTPException(status_code=404, detail="Dianxiaomi handoff not found")
    return handoff


def _find_extension_product(db: Session, payload: BrowserExtensionCollectionRequest) -> Product:
    normalized_url = normalize_aliexpress_url(payload.url, "product")
    url_product_id = extract_product_id(normalized_url)
    raw_product_id = payload.raw_data.get("monitored_product_id")
    submitted_ids = {
        value
        for value in (payload.platform_product_id, url_product_id, raw_product_id)
        if isinstance(value, str) and value.isdigit()
    }
    products = list(
        db.scalars(
            select(Product)
            .join(Store, Store.id == Product.store_id)
            .where(
                Product.aliexpress_product_id.in_(submitted_ids),
                Product.status == "active",
                Store.status == "active",
            )
            .limit(2)
        )
    )
    if not products:
        # AliExpress may redirect a product URL and expose a different public
        # identifier than the one embedded in the original store listing.
        # Match only an already monitored URL; never create a product here.
        products = list(
            db.scalars(
                select(Product)
                .join(Store, Store.id == Product.store_id)
                .where(
                    Product.url == normalized_url,
                    Product.status == "active",
                    Store.status == "active",
                )
                .limit(2)
            )
        )
    if not products:
        raise HTTPException(status_code=404, detail="该商品尚未加入监控")
    if len(products) > 1:
        raise HTTPException(status_code=409, detail="该商品 ID 对应多个监控商品，请使用手动输入渠道")
    return products[0]


def _save_channel_observation(
    db: Session,
    payload: BrowserExtensionCollectionRequest,
    source: str,
    *,
    commit: bool = True,
    expected_product: Product | None = None,
) -> ProductSnapshot:
    product = expected_product or _find_extension_product(db, payload)
    captured_at = payload.captured_at or datetime.now(ZoneInfo(get_settings().timezone))
    parse_status = "success" if payload.sold_count is not None else "partial"
    raw_data = dict(payload.raw_data)
    if payload.historical_sales:
        raw_data["historical_sales"] = [
            point.model_dump(mode="json") for point in payload.historical_sales
        ]
    result = CollectorResult(
        collected_at=captured_at,
        collector_name=source.lower(),
        collector_version=str(payload.raw_data.get("extractor_version", "1.0")),
        parse_status=parse_status,
        cumulative_sold=payload.sold_count,
        price_amount=payload.price,
        price_currency=None,
        review_count=payload.review_count,
        title=payload.title,
        source_http_status=200,
        raw_payload=raw_data,
        raw_content=None,
        error_type=None if payload.sold_count is not None else "missing_sold_count",
        error_message=None if payload.sold_count is not None else "公开页面未提供累计销量",
    )
    snapshot = save_external_snapshot(db, product, result, source, commit=False)
    imported_dates = import_historical_sales(
        db,
        product.id,
        snapshot.id,
        payload.historical_sales,
        commit=False,
    )
    metrics = calculate_product_daily_metrics(
        db,
        product.id,
        timezone_name=get_settings().timezone,
        commit=False,
    )
    metric_dates = set(imported_dates)
    if metrics:
        metric_dates.add(metrics[-1].metric_date)
    for metric_date in metric_dates:
        calculate_store_daily_metric(
            db, product.store_id, metric_date, commit=False
        )
    if commit:
        db.commit()
        db.refresh(snapshot)
    return snapshot


def _save_store_observation(
    db: Session,
    payload: BrowserStoreDiscoveryRequest,
    *,
    commit: bool = True,
) -> dict:
    store = db.scalar(
        select(Store).where(Store.aliexpress_store_id == payload.platform_store_id)
    )
    if store is None:
        raise HTTPException(status_code=404, detail="该店铺尚未加入监控")
    if store.status != "active":
        raise HTTPException(status_code=409, detail="该店铺监控已停用")

    captured_at = datetime.now(ZoneInfo(get_settings().timezone))
    normalized_store_url = normalize_aliexpress_url(payload.url, "store")
    links: list[str] = []
    added_count = 0
    existing_count = 0
    deactivated_count = 0
    skipped_count = 0
    observed_products: list[tuple[Product, object, int]] = []
    for rank, candidate in enumerate(payload.products, start=1):
        normalized_url = normalize_aliexpress_url(candidate.url, "product")
        if extract_product_id(normalized_url) != candidate.platform_product_id:
            skipped_count += 1
            continue
        links.append(normalized_url)
        product = db.scalar(
            select(Product).where(
                Product.store_id == store.id,
                Product.aliexpress_product_id == candidate.platform_product_id,
            )
        )
        if product is not None:
            if product.discovery_source in ("store_page", "chrome_extension_store"):
                product.status = "active"
            product.discovery_rank = rank
            product.discovered_at = captured_at
            if candidate.title and not product.title:
                product.title = candidate.title
            observed_products.append((product, candidate, rank))
            existing_count += 1
            continue
        conflicting = db.scalar(select(Product).where(Product.url == normalized_url))
        if conflicting is not None:
            skipped_count += 1
            continue
        product = Product(
            store_id=store.id,
            aliexpress_product_id=candidate.platform_product_id,
            url=normalized_url,
            title=candidate.title,
            discovery_source="chrome_extension_store",
            discovery_rank=rank,
            discovered_at=captured_at,
        )
        db.add(product)
        db.flush()
        observed_products.append((product, candidate, rank))
        added_count += 1

    current_ids = {extract_product_id(link) for link in links}
    if current_ids:
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

    snapshot = StoreSnapshot(
        store_id=store.id,
        collected_at=captured_at,
        collector_name="chrome_extension_store",
        collector_version=str(payload.raw_data.get("extractor_version", "1.0")),
        parse_status="success" if links else "failed",
        candidate_count=len(payload.products),
        source_http_status=200,
        raw_payload={
            "store_url": normalized_store_url,
            "products": [item.model_dump() for item in payload.products],
            "raw_data": payload.raw_data,
        },
        error_type=None if links else "no_valid_product_links",
        error_message=None if links else "没有读取到有效商品链接",
    )
    db.add(snapshot)
    db.flush()
    metric_date = captured_at.astimezone(ZoneInfo(get_settings().timezone)).date()
    for product, candidate, rank in observed_products:
        save_store_product_snapshot(
            db,
            product,
            captured_at=captured_at,
            sold_count=candidate.public_cumulative_sold,
            title=candidate.title,
            source="CHROME_EXTENSION",
            collector_name="chrome_extension_store",
            collector_version=str(payload.raw_data.get("extractor_version", "1.0")),
            raw_payload={
                "collection_mode": "store_page",
                "store_snapshot_id": snapshot.id,
                "rank": rank,
                "product_id": candidate.platform_product_id,
                "url": normalized_url,
                "title": candidate.title,
                "public_cumulative_sold": candidate.public_cumulative_sold,
                "extension_raw_data": payload.raw_data,
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
    if commit:
        db.commit()
    else:
        db.flush()
    return {
        "store_id": store.id,
        "discovered_count": len(payload.products),
        "added_count": added_count,
        "existing_count": existing_count,
        "deactivated_count": deactivated_count,
        "skipped_count": skipped_count,
        "product_links": links,
        "status": "success" if links else "failed",
        "error_message": None if links else "没有读取到有效商品链接",
    }


def commit_or_conflict(db: Session, detail: str) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail) from exc


@router.post("/stores", response_model=StoreRead, status_code=status.HTTP_201_CREATED)
def create_store(payload: StoreCreate, db: Session = Depends(get_db)) -> Store:
    normalized_url = normalize_aliexpress_url(payload.url, "store")
    external_id = payload.aliexpress_store_id or extract_store_id(normalized_url)
    store = Store(
        name=(payload.name or "").strip() or f"AliExpress Store {external_id or 'Unlabeled'}",
        url=normalized_url,
        aliexpress_store_id=external_id,
    )
    db.add(store)
    commit_or_conflict(db, "Store URL or AliExpress store ID already exists")
    db.refresh(store)
    return store


@router.get("/stores", response_model=list[StoreRead])
def list_stores(
    status_filter: str | None = Query(default=None, alias="status", pattern="^(active|inactive)$"),
    db: Session = Depends(get_db),
) -> list[Store]:
    statement = select(Store).order_by(Store.id)
    if status_filter:
        statement = statement.where(Store.status == status_filter)
    return list(db.scalars(statement))


@router.get("/stores/{store_id}", response_model=StoreRead)
def get_store(store_id: int, db: Session = Depends(get_db)) -> Store:
    store = db.get(Store, store_id)
    if store is None:
        raise HTTPException(status_code=404, detail="Store not found")
    return store


@router.post(
    "/collection/browser-extension",
    response_model=ProductSnapshotRead,
    status_code=status.HTTP_201_CREATED,
)
def collect_from_browser_extension(
    payload: BrowserExtensionCollectionRequest,
    db: Session = Depends(get_db),
) -> ProductSnapshot:
    return _save_channel_observation(db, payload, "CHROME_EXTENSION")


@router.post(
    "/collection/browser-extension/store",
    response_model=BrowserStoreDiscoveryRead,
    status_code=status.HTTP_201_CREATED,
)
def discover_store_from_browser_extension(
    payload: BrowserStoreDiscoveryRequest,
    db: Session = Depends(get_db),
) -> dict:
    return _save_store_observation(db, payload)


@router.post(
    "/collection/manual",
    response_model=ProductSnapshotRead,
    status_code=status.HTTP_201_CREATED,
)
def collect_from_manual_input(
    payload: ManualCollectionRequest,
    db: Session = Depends(get_db),
) -> ProductSnapshot:
    return _save_channel_observation(db, payload, "MANUAL")


@router.get("/collection/pending", response_model=list[CollectionPendingRead])
def list_pending_manual_collection(db: Session = Depends(get_db)) -> list[dict]:
    tasks = list(
        db.scalars(
            select(ManualCollectionTask)
            .join(Product, Product.id == ManualCollectionTask.product_id)
            .join(Store, Store.id == Product.store_id)
            .where(ManualCollectionTask.status == "PENDING", Product.status == "active")
            .order_by(ManualCollectionTask.created_at.desc())
        )
    )
    rows: list[dict] = []
    for task in tasks:
        product = task.product
        latest_success = db.scalar(
            select(ProductSnapshot)
            .where(
                ProductSnapshot.product_id == product.id,
                ProductSnapshot.status == "VALID",
            )
            .order_by(ProductSnapshot.captured_at.desc(), ProductSnapshot.id.desc())
            .limit(1)
        )
        rows.append(
            {
                "product_id": product.id,
                "platform_product_id": product.aliexpress_product_id,
                "title": product.title,
                "product_url": product.url,
                "store_id": product.store_id,
                "store_name": product.store.name,
                "last_success_at": latest_success.captured_at if latest_success else None,
                "failed_at": task.last_attempt.attempted_at if task.last_attempt else None,
                "failure_reason": task.reason,
            }
        )
    return rows


@router.get("/collection/attempts", response_model=list[CollectionAttemptRead])
def list_collection_attempts(
    product_id: int | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[CollectionAttempt]:
    statement = select(CollectionAttempt).order_by(CollectionAttempt.attempted_at.desc()).limit(limit)
    if product_id is not None:
        statement = statement.where(CollectionAttempt.product_id == product_id)
    return list(db.scalars(statement))


@router.post(
    "/integrations/dianxiaomi/handoffs",
    response_model=DianxiaomiHandoffBatchRead,
    status_code=status.HTTP_201_CREATED,
)
def create_dianxiaomi_handoffs(
    payload: DianxiaomiHandoffCreate,
    db: Session = Depends(get_db),
) -> dict:
    try:
        batch_id, rows = create_handoff_batch(db, payload.product_ids)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    requested_count = len(dict.fromkeys(payload.product_ids))
    return {
        "batch_id": batch_id,
        "total_count": requested_count,
        "queued_count": len(rows),
        "reused_count": requested_count - len(rows),
        "items": [serialize_handoff(row) for row in rows],
    }


@router.get(
    "/integrations/dianxiaomi/handoffs",
    response_model=list[DianxiaomiHandoffRead],
)
def list_dianxiaomi_handoffs(
    batch_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[dict]:
    statement = select(DianxiaomiHandoff).order_by(
        DianxiaomiHandoff.requested_at.desc(), DianxiaomiHandoff.id.desc()
    ).limit(limit)
    if batch_id:
        statement = statement.where(DianxiaomiHandoff.batch_id == batch_id)
    return [serialize_handoff(row) for row in db.scalars(statement)]


@router.get("/integrations/dianxiaomi/status")
def dianxiaomi_handoff_status(db: Session = Depends(get_db)) -> dict:
    rows = list(
        db.scalars(
            select(DianxiaomiHandoff)
            .order_by(DianxiaomiHandoff.requested_at.desc(), DianxiaomiHandoff.id.desc())
            .limit(500)
        )
    )
    counts = {
        "queued": 0,
        "processing": 0,
        "submitted": 0,
        "succeeded": 0,
        "failed": 0,
        "needs_confirmation": 0,
    }
    for row in rows:
        if row.status == "QUEUED":
            counts["queued"] += 1
        elif row.status == "COLLECTING":
            counts["submitted"] += 1
            counts["processing"] += 1
        elif row.status in {"CLAIMED", "OPENED", "FILLED"}:
            counts["processing"] += 1
        elif row.status == "SUCCEEDED":
            counts["succeeded"] += 1
        elif row.status == "FAILED":
            counts["failed"] += 1
        elif row.status == "NEEDS_CONFIRMATION":
            counts["needs_confirmation"] += 1
    return {
        **counts,
        "latest_at": rows[0].requested_at if rows else None,
        "items": [serialize_handoff(row) for row in rows[:50]],
        "latest": [serialize_handoff(row) for row in rows[:10]],
    }


@router.post(
    "/integrations/dianxiaomi/handoffs/claim-next",
    response_model=DianxiaomiHandoffRead | None,
)
def claim_next_dianxiaomi_handoff(
    payload: DianxiaomiHandoffClaimRequest,
    db: Session = Depends(get_db),
) -> dict | None:
    row = claim_next_handoff(db, payload.worker_id)
    return serialize_handoff(row) if row else None


@router.post(
    "/integrations/dianxiaomi/handoffs/claim-batch",
    response_model=list[DianxiaomiHandoffRead],
)
def claim_dianxiaomi_handoff_batch(
    payload: DianxiaomiHandoffClaimRequest,
    db: Session = Depends(get_db),
) -> list[dict]:
    rows = claim_handoff_batch(
        db,
        payload.worker_id,
        limit=payload.limit,
        resume_confirmed=payload.resume_confirmed,
    )
    return [serialize_handoff(row) for row in rows]


@router.post(
    "/integrations/dianxiaomi/handoffs/{handoff_id}/claim",
    response_model=DianxiaomiHandoffRead,
)
def claim_dianxiaomi_handoff(
    handoff_id: int,
    payload: DianxiaomiHandoffClaimRequest,
    db: Session = Depends(get_db),
) -> dict:
    row = _handoff_or_404(db, handoff_id)
    if row.status != "QUEUED":
        raise HTTPException(status_code=409, detail="该店小秘任务已被领取或已完成")
    row.status = "CLAIMED"
    row.claimed_at = datetime.now(timezone.utc)
    row.worker_id = payload.worker_id
    db.commit()
    db.refresh(row)
    return serialize_handoff(row)


@router.post(
    "/integrations/dianxiaomi/handoffs/{handoff_id}/status",
    response_model=DianxiaomiHandoffRead,
)
def update_dianxiaomi_handoff_status(
    handoff_id: int,
    payload: DianxiaomiHandoffStatusRequest,
    db: Session = Depends(get_db),
) -> dict:
    row = _handoff_or_404(db, handoff_id)
    if row.status not in ACTIVE_HANDOFF_STATUSES and payload.status != "CANCELED":
        raise HTTPException(status_code=409, detail="该店小秘任务已结束")
    if row.worker_id and payload.worker_id and row.worker_id != payload.worker_id:
        raise HTTPException(status_code=409, detail="店小秘任务不属于当前扩展实例")
    return serialize_handoff(
        update_handoff(
            db,
            row,
            payload.status,
            error_type=payload.error_type,
            error_message=payload.error_message,
            worker_id=payload.worker_id,
        )
    )


@router.post(
    "/integrations/dianxiaomi/handoffs/{handoff_id}/cancel",
    response_model=DianxiaomiHandoffRead,
)
def cancel_dianxiaomi_handoff(
    handoff_id: int,
    db: Session = Depends(get_db),
) -> dict:
    row = _handoff_or_404(db, handoff_id)
    if row.status in {"SUCCEEDED", "FAILED", "CANCELED"}:
        raise HTTPException(status_code=409, detail="该店小秘任务已结束")
    return serialize_handoff(update_handoff(db, row, "CANCELED"))


@router.post(
    "/integrations/dianxiaomi/handoffs/{handoff_id}/resume",
    response_model=DianxiaomiHandoffRead,
)
def resume_dianxiaomi_handoff(
    handoff_id: int,
    db: Session = Depends(get_db),
) -> dict:
    row = _handoff_or_404(db, handoff_id)
    if row.status not in {"NEEDS_CONFIRMATION", "FAILED"}:
        raise HTTPException(status_code=409, detail="该店小秘任务当前不能重新排队")
    row.status = "QUEUED"
    row.worker_id = None
    row.error_type = None
    row.error_message = None
    row.completed_at = None
    db.commit()
    db.refresh(row)
    return serialize_handoff(row)


@router.get("/collection/status/today", response_model=CollectionStatusRead)
def collection_status_today(db: Session = Depends(get_db)) -> dict:
    today = datetime.now(ZoneInfo(get_settings().timezone)).date()
    active_products = list(
        db.scalars(
            select(Product)
            .join(Store, Store.id == Product.store_id)
            .where(Product.status == "active", Store.status == "active")
        )
    )
    attempts = list(db.scalars(select(CollectionAttempt).where(CollectionAttempt.attempted_at >= datetime.combine(today, datetime.min.time(), tzinfo=ZoneInfo(get_settings().timezone)))))
    auto_success = {attempt.product_id for attempt in attempts if attempt.source == "AUTO" and attempt.status != "FAILED"}
    auto_failed = {attempt.product_id for attempt in attempts if attempt.source == "AUTO" and attempt.status == "FAILED"}
    day_start = datetime.combine(today, datetime.min.time(), tzinfo=ZoneInfo(get_settings().timezone))
    manual_completed = manual_completed_product_ids(db, day_start)
    manual_completed.update(
        task.product_id
        for task in db.scalars(
            select(ManualCollectionTask).where(
                ManualCollectionTask.status == "COMPLETED",
                ManualCollectionTask.completed_at >= day_start,
            )
        )
    )
    pending_manual = db.scalar(
        select(func.count())
        .select_from(ManualCollectionTask)
        .join(Product, Product.id == ManualCollectionTask.product_id)
        .where(ManualCollectionTask.status == "PENDING", Product.status == "active")
    ) or 0
    total = len(active_products)
    return {
        "date": today,
        "total_products": total,
        "auto_success": len(auto_success),
        "auto_failed": len(auto_failed),
        "manual_completed": len(manual_completed),
        "pending_manual": pending_manual,
        "success_rate": round((len(auto_success) / total) * 100, 2) if total else 0.0,
    }


@router.get("/collection/progress", response_model=CollectionProgressRead)
def collection_progress(db: Session = Depends(get_db)) -> dict:
    return build_collection_progress(db)


def _browser_collection_item(
    db: Session, item_id: int, target_type: str | None = None
) -> BrowserCollectionItem:
    item = db.get(BrowserCollectionItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Browser collection item not found")
    if target_type and item.target_type != target_type:
        raise HTTPException(status_code=409, detail="Browser collection item type mismatch")
    if item.status not in {"RUNNING", "NEEDS_VERIFICATION"}:
        raise HTTPException(status_code=409, detail="Browser collection item is not active")
    return item


@router.get(
    "/browser-collection/runs/today",
    response_model=BrowserCollectionRunRead | None,
)
def browser_collection_today(db: Session = Depends(get_db)) -> dict | None:
    run = get_today_run(db)
    return serialize_run(run) if run else None


@router.post(
    "/browser-collection/runs/ensure",
    response_model=BrowserCollectionRunRead,
)
def browser_collection_ensure(
    payload: BrowserCollectionRunEnsureRequest,
    db: Session = Depends(get_db),
) -> dict:
    return serialize_run(
        ensure_browser_collection_run(db, payload.target_date, payload.trigger)
    )


@router.post(
    "/browser-collection/items/claim",
    response_model=BrowserCollectionClaimRead,
)
def browser_collection_claim(db: Session = Depends(get_db)) -> dict:
    run, item = claim_next_item(db)
    return {
        "run": serialize_run(run),
        "item": serialize_item(item) if item else None,
    }


@router.post(
    "/browser-collection/items/{item_id}/snapshot",
    response_model=BrowserCollectionRunRead,
)
def browser_collection_product_snapshot(
    item_id: int,
    payload: BrowserExtensionCollectionRequest,
    db: Session = Depends(get_db),
) -> dict:
    item = _browser_collection_item(db, item_id, "PRODUCT")
    product = item.product
    if product is None:
        raise HTTPException(status_code=409, detail="Browser product task has no product")
    scheduled_payload = payload.model_copy(
        update={
            "raw_data": {
                **payload.raw_data,
                "collection_mode": "scheduled",
                "browser_collection_item_id": item.id,
                "monitored_product_id": product.aliexpress_product_id,
            }
        }
    )
    snapshot = _save_channel_observation(
        db,
        scheduled_payload,
        "CHROME_EXTENSION",
        commit=False,
        expected_product=product,
    )
    if snapshot.product_id != item.product_id:
        db.rollback()
        raise HTTPException(status_code=409, detail="Snapshot product does not match task")
    run = finish_item(
        db,
        item,
        "SUCCEEDED" if snapshot.status == "VALID" else "PARTIAL",
        snapshot_id=snapshot.id,
        error_type=snapshot.error_type,
        error_message=snapshot.error_message,
        commit=False,
    )
    db.commit()
    db.refresh(run)
    return serialize_run(run)


@router.post(
    "/browser-collection/items/{item_id}/store-discovery",
    response_model=BrowserCollectionRunRead,
)
def browser_collection_store_discovery(
    item_id: int,
    payload: BrowserStoreDiscoveryRequest,
    db: Session = Depends(get_db),
) -> dict:
    item = _browser_collection_item(db, item_id, "STORE")
    result = _save_store_observation(db, payload, commit=False)
    if result["store_id"] != item.store_id:
        db.rollback()
        raise HTTPException(status_code=409, detail="Store result does not match task")
    run = finish_item(
        db,
        item,
        "SUCCEEDED" if result["status"] == "success" else "FAILED",
        error_type=None if result["status"] == "success" else "no_valid_product_links",
        error_message=result["error_message"],
        commit=False,
    )
    db.commit()
    db.refresh(run)
    return serialize_run(run)


@router.post(
    "/browser-collection/items/{item_id}/failure",
    response_model=BrowserCollectionRunRead,
)
def browser_collection_failure(
    item_id: int,
    payload: BrowserCollectionFailureRequest,
    db: Session = Depends(get_db),
) -> dict:
    item = _browser_collection_item(db, item_id)
    return serialize_run(
        fail_item(db, item, payload.error_type, payload.error_message)
    )


@router.post(
    "/browser-collection/items/{item_id}/challenge",
    response_model=BrowserCollectionRunRead,
)
def browser_collection_challenge(
    item_id: int,
    payload: BrowserCollectionChallengeRequest,
    db: Session = Depends(get_db),
) -> dict:
    item = _browser_collection_item(db, item_id)
    return serialize_run(pause_for_challenge(db, item, payload.error_message))


@router.post(
    "/browser-collection/runs/{run_id}/resume",
    response_model=BrowserCollectionRunRead,
)
def browser_collection_resume(
    run_id: int,
    db: Session = Depends(get_db),
) -> dict:
    run = db.get(BrowserCollectionRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Browser collection run not found")
    if run.status != "NEEDS_VERIFICATION":
        raise HTTPException(status_code=409, detail="Browser collection run is not paused")
    return serialize_run(resume_run(db, run))


@router.post(
    "/browser-collection/heartbeat",
    response_model=BrowserCollectionRunRead,
)
def browser_collection_heartbeat(
    _payload: BrowserCollectionHeartbeatRequest,
    db: Session = Depends(get_db),
) -> dict:
    return serialize_run(record_heartbeat(db))


@router.patch("/stores/{store_id}", response_model=StoreRead)
def update_store(store_id: int, payload: StoreUpdate, db: Session = Depends(get_db)) -> Store:
    store = db.get(Store, store_id)
    if store is None:
        raise HTTPException(status_code=404, detail="Store not found")
    updates = payload.model_dump(exclude_unset=True)
    if "url" in updates:
        updates["url"] = normalize_aliexpress_url(updates["url"], "store")
    for field, value in updates.items():
        setattr(store, field, value.strip() if isinstance(value, str) else value)
    commit_or_conflict(db, "Store URL or AliExpress store ID already exists")
    db.refresh(store)
    return store


@router.post("/stores/{store_id}/deactivate", response_model=StoreRead)
def deactivate_store(store_id: int, db: Session = Depends(get_db)) -> Store:
    return update_store(store_id, StoreUpdate(status="inactive"), db)


@router.post(
    "/stores/{store_id}/discover",
    response_model=StoreDiscoverySummaryRead,
)
def discover_store(
    store_id: int,
    limit: int = Query(default=20, ge=1, le=500),
    db: Session = Depends(get_db),
    collector: StoreDiscoveryCollector = Depends(get_store_discovery_collector),
) -> dict:
    store = db.get(Store, store_id)
    if store is None:
        raise HTTPException(status_code=404, detail="Store not found")
    if store.status != "active":
        raise HTTPException(status_code=409, detail="Store monitoring is inactive")
    return discover_store_products(db, store, collector, limit).to_dict()


@router.get("/stores/{store_id}/discovery-snapshots", response_model=list[StoreSnapshotRead])
def list_store_discovery_snapshots(
    store_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> list[StoreSnapshot]:
    if db.get(Store, store_id) is None:
        raise HTTPException(status_code=404, detail="Store not found")
    return list(
        db.scalars(
            select(StoreSnapshot)
            .where(StoreSnapshot.store_id == store_id)
            .order_by(StoreSnapshot.collected_at.desc(), StoreSnapshot.id.desc())
            .limit(limit)
        )
    )


@router.get("/stores/{store_id}/top-products", response_model=StoreTopProductsRead)
def get_store_top_products(
    store_id: int,
    metric_date: date | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    if db.get(Store, store_id) is None:
        raise HTTPException(status_code=404, detail="Store not found")
    target_date = metric_date or (
        datetime.now(ZoneInfo(get_settings().timezone)).date() - timedelta(days=1)
    )
    rows = store_top_products(db, store_id, target_date, limit)
    return {
        "store_id": store_id,
        "metric_date": target_date,
        "status": "ready" if rows else "awaiting_daily_baseline",
        "products": [
            TopProductRead(
                rank=rank,
                product_id=product.id,
                aliexpress_product_id=product.aliexpress_product_id,
                title=product.title,
                url=product.url,
                estimated_sales=metric.estimated_sales or 0,
            )
            for rank, (metric, product) in enumerate(rows, start=1)
        ],
    }


@router.post("/products", response_model=ProductRead, status_code=status.HTTP_201_CREATED)
def create_product(payload: ProductCreate, db: Session = Depends(get_db)) -> Product:
    if payload.store_id is None:
        store = db.scalar(
            select(Store).where(
                Store.name == "自定义监控",
                Store.aliexpress_store_id.is_(None),
            )
        )
        if store is None:
            store = Store(
                name="自定义监控",
                url="https://www.aliexpress.com/",
                aliexpress_store_id=None,
            )
            db.add(store)
            db.flush()
    else:
        store = db.get(Store, payload.store_id)
        if store is None:
            raise HTTPException(status_code=404, detail="Store not found")
    if store.status != "active":
        raise HTTPException(status_code=409, detail="Cannot add a product to an inactive store")

    normalized_url = (
        normalize_aliexpress_url(payload.url, "product")
        if payload.url
        else canonical_product_url(payload.aliexpress_product_id or "")
    )
    product_id = payload.aliexpress_product_id or extract_product_id(normalized_url)
    if payload.url and payload.aliexpress_product_id and extract_product_id(normalized_url) != product_id:
        raise HTTPException(status_code=422, detail="Product ID does not match product URL")
    if not product_id.isdigit():
        raise HTTPException(status_code=422, detail="AliExpress product ID must be numeric")

    product = Product(
        store_id=store.id,
        aliexpress_product_id=product_id,
        url=normalized_url,
        title=payload.title,
    )
    db.add(product)
    commit_or_conflict(db, "Product URL or AliExpress product ID already exists for this store")
    db.refresh(product)
    return product


@router.get("/products", response_model=list[ProductRead])
def list_products(
    store_id: int | None = None,
    status_filter: str | None = Query(default=None, alias="status", pattern="^(active|inactive)$"),
    db: Session = Depends(get_db),
) -> list[Product]:
    statement = select(Product).order_by(Product.id)
    if store_id is not None:
        statement = statement.where(Product.store_id == store_id)
    if status_filter:
        statement = statement.where(Product.status == status_filter)
    return list(db.scalars(statement))


@router.get("/products/{product_id}", response_model=ProductRead)
def get_product(product_id: int, db: Session = Depends(get_db)) -> Product:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


@router.patch("/products/{product_id}", response_model=ProductRead)
def update_product(
    product_id: int, payload: ProductUpdate, db: Session = Depends(get_db)
) -> Product:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(product, field, value)
    db.commit()
    db.refresh(product)
    return product


@router.post("/products/{product_id}/deactivate", response_model=ProductRead)
def deactivate_product(product_id: int, db: Session = Depends(get_db)) -> Product:
    return update_product(product_id, ProductUpdate(status="inactive"), db)


@router.post("/products/{product_id}/collect", response_model=ProductSnapshotRead)
def collect_product(
    product_id: int,
    db: Session = Depends(get_db),
    collector: Collector = Depends(get_collector),
) -> ProductSnapshot:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    if product.status != "active":
        raise HTTPException(status_code=409, detail="Product monitoring is inactive")
    snapshot = collect_product_snapshot(db, product, collector)
    metrics = calculate_product_daily_metrics(db, product.id)
    if metrics:
        calculate_store_daily_metric(db, product.store_id, metrics[-1].metric_date)
    return snapshot


@router.get("/products/{product_id}/snapshots", response_model=list[ProductSnapshotRead])
def list_product_snapshots(
    product_id: int,
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[ProductSnapshot]:
    if db.get(Product, product_id) is None:
        raise HTTPException(status_code=404, detail="Product not found")
    statement = (
        select(ProductSnapshot)
        .where(ProductSnapshot.product_id == product_id)
        .order_by(ProductSnapshot.collected_at.desc(), ProductSnapshot.id.desc())
        .limit(limit)
    )
    return list(db.scalars(statement))


@router.get(
    "/products/{product_id}/daily-metrics", response_model=list[ProductDailyMetricRead]
)
def list_product_daily_metrics(
    product_id: int,
    limit: int = Query(default=90, ge=1, le=365),
    db: Session = Depends(get_db),
) -> list[ProductDailyMetric]:
    if db.get(Product, product_id) is None:
        raise HTTPException(status_code=404, detail="Product not found")
    statement = (
        select(ProductDailyMetric)
        .where(ProductDailyMetric.product_id == product_id)
        .order_by(ProductDailyMetric.metric_date.desc())
        .limit(limit)
    )
    return list(db.scalars(statement))


@router.get("/stores/{store_id}/daily-metrics", response_model=list[StoreDailyMetricRead])
def list_store_daily_metrics(
    store_id: int,
    limit: int = Query(default=90, ge=1, le=365),
    db: Session = Depends(get_db),
) -> list[StoreDailyMetric]:
    if db.get(Store, store_id) is None:
        raise HTTPException(status_code=404, detail="Store not found")
    statement = (
        select(StoreDailyMetric)
        .where(StoreDailyMetric.store_id == store_id)
        .order_by(StoreDailyMetric.metric_date.desc())
        .limit(limit)
    )
    return list(db.scalars(statement))


@router.post("/jobs/collect-now")
def collect_all_now(
    db: Session = Depends(get_db), collector: Collector = Depends(get_collector)
) -> dict:
    return run_collection_cycle(db, collector).to_dict()
