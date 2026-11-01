from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.api.schemas import (
    BrowserCollectionChallengeRequest,
    BrowserCollectionClaimRequest,
    BrowserCollectionClaimRead,
    BrowserCollectionControlRead,
    BrowserCollectionFailureRequest,
    BrowserCollectionHeartbeatRequest,
    BrowserCollectionIdentityReviewRequest,
    StorePoolResetBatchRead,
    StorePoolResetChallengeRequest,
    StorePoolResetClaimRead,
    StorePoolResetClaimRequest,
    StorePoolResetCreateRequest,
    StorePoolResetFailureRequest,
    StorePoolResetHeartbeatRequest,
    StorePoolResetPageRequest,
    StorePoolResetResumeRequest,
    BrowserCollectionRunEnsureRequest,
    BrowserCollectionRunResumeRequest,
    BrowserCollectionRunRead,
    BrowserCollectionRunStartRequest,
    BrowserCollectionWorkerSelectRequest,
    BrowserExtensionCollectionRequest,
    DianxiaomiHandoffBatchRead,
    DianxiaomiHandoffClaimRequest,
    DianxiaomiHandoffCreate,
    DianxiaomiHandoffRead,
    DianxiaomiHandoffStatusRequest,
    DianxiaomiQueueHeartbeatRequest,
    DianxiaomiWorkerHandoffRead,
    DianxiaomiWorkerStatusRequest,
    BrowserStoreDiscoveryRead,
    BrowserStoreDiscoveryRequest,
    CollectionAttemptRead,
    CollectionProgressRead,
    CollectionPendingRead,
    CollectionStatusRead,
    ManualCollectionRequest,
    ProductCreate,
    ProductMonitorClearRequest,
    ProductMonitorClearResponse,
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
    SelectionCandidatesBatchImport,
    SelectionQualificationRuleRead,
    SelectionQualificationRuleWrite,
)
from app.collectors.base import Collector, CollectorResult
from app.collectors.dependencies import get_collector
from app.collectors.store_dependencies import get_store_discovery_collector
from app.collectors.store_discovery import StoreDiscoveryCollector
from app.core.auth import (
    is_configured_admin_session,
    require_admin,
    require_authenticated,
    require_dianxiaomi_admin,
)
from app.core.config import get_settings
from app.api.service_auth import (
    require_dianxiaomi_service_token,
    require_selection_service_token,
)
from app.api.utils import (
    canonical_product_url,
    extract_product_id,
    extract_store_id,
    normalize_aliexpress_url,
)
from app.db.models import (
    BrowserCollectionControl,
    BrowserCollectionItem,
    BrowserCollectionRun,
    BrowserCollectionWorker,
    CollectionAttempt,
    DianxiaomiHandoff,
    DianxiaomiQueueControl,
    ManualCollectionTask,
    Product,
    ProductDailyMetric,
    ProductSnapshot,
    Store,
    StoreDailyMetric,
    StoreSnapshot,
    SelectionCandidate,
    SelectionQualificationRule,
)
from app.db.session import get_db
from app.services.snapshots import collect_product_snapshot
from app.services.snapshots import save_external_snapshot, save_store_product_snapshot
from app.services.collection_runs import run_collection_cycle
from app.services.metrics import calculate_product_daily_metrics, calculate_store_daily_metric
from app.services.historical_metrics import (
    import_historical_sales,
    looks_cumulative_like_daily_values,
)
from app.services.monitored_store_collection import (
    StoreIdentityValidationError,
    save_monitored_store_observation,
)
from app.services.store_page_discovery import save_store_page_candidates
from app.services.product_presentation import clean_product_title
from app.services.store_discovery import discover_store_products, store_top_products
from app.services.collection_progress import build_collection_progress, manual_completed_product_ids
from app.services.browser_collection import (
    browser_collection_control_status,
    cancel_run,
    claim_next_item,
    collection_window_end_date,
    confirm_store_identity_review,
    ensure_browser_collection_run,
    fail_item,
    finish_item,
    get_today_run,
    pause_for_challenge,
    pause_run,
    queue_all_store_collections,
    queue_single_product_collection,
    queue_store_collection,
    record_heartbeat,
    record_store_identity_review,
    reject_store_identity_review,
    resume_run,
    retry_item,
    select_browser_collection_worker,
    serialize_item,
    serialize_run,
    start_browser_collection_run,
    validate_browser_collection_claim,
)
from app.services.store_pool_reset import (
    cancel_pool_reset_batch,
    claim_pool_reset_store,
    create_pool_reset_batch,
    get_latest_pool_reset_batch,
    record_pool_reset_challenge,
    record_pool_reset_failure,
    record_pool_reset_heartbeat,
    record_pool_reset_page,
    resume_pool_reset_after_challenge,
    retry_pool_reset_store,
    serialize_pool_reset_batch,
    start_pool_reset_batch,
)
from app.services.dianxiaomi import (
    ACTIVE_HANDOFF_STATUSES,
    LEASED_HANDOFF_STATUSES,
    claim_handoff,
    claim_handoff_batch,
    claim_next_handoff,
    confirm_product_page_identity,
    create_handoff_batch,
    delete_failed_handoffs,
    pause_dianxiaomi_queue,
    record_dianxiaomi_heartbeat,
    reject_product_page_identity,
    resume_dianxiaomi_queue,
    retry_handoff,
    serialize_handoff,
    update_handoff,
)
from app.services.selection_qualification import (
    evaluate_all_selection_candidates,
    evaluate_selection_candidate,
    import_selection_candidates,
    list_selection_candidates,
    serialize_selection_candidate,
    set_selection_rule,
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
    daily_target_date: date | None = None,
) -> ProductSnapshot:
    product = expected_product or _find_extension_product(db, payload)
    local_timezone = ZoneInfo(get_settings().timezone)
    captured_at = payload.captured_at or datetime.now(timezone.utc)
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=local_timezone)
    captured_at = captured_at.astimezone(timezone.utc)
    target_date = daily_target_date
    if target_date is None:
        raw_target = payload.raw_data.get("history_target_date")
        try:
            target_date = date.fromisoformat(str(raw_target)) if raw_target else captured_at.astimezone(local_timezone).date()
        except ValueError:
            target_date = captured_at.astimezone(local_timezone).date()
    expected_dates = {target_date - timedelta(days=offset) for offset in range(1, 8)}
    daily_points_by_date = {
        point.date: point
        for point in payload.historical_sales
        if point.value_type == "daily_increment" and point.date in expected_dates
    }
    daily_points = [daily_points_by_date[value] for value in sorted(daily_points_by_date)]
    daily_capture_complete = set(daily_points_by_date) == expected_dates
    raw_data = dict(payload.raw_data)
    for key in (
        "sales_collection_source",
        "sales_count_type",
        "sales_count_captured",
        "sales_count_source_text",
        "sales_count_approximate",
        "visible_text_excerpt",
        "public_cumulative_sold",
    ):
        raw_data.pop(key, None)
    raw_data["historical_sales"] = [point.model_dump(mode="json") for point in daily_points]
    raw_data["history_daily_capture_complete"] = daily_capture_complete
    raw_data["history_sampling_strategy"] = "latest_7_daily_increments_only"
    result = CollectorResult(
        collected_at=captured_at,
        collector_name=source.lower(),
        collector_version=str(payload.raw_data.get("extractor_version", "1.0")),
        parse_status="success" if daily_capture_complete else "partial",
        cumulative_sold=None,
        price_amount=payload.price,
        price_currency=None,
        review_count=payload.review_count,
        title=clean_product_title(payload.title),
        source_http_status=200,
        raw_payload=raw_data,
        raw_content=None,
        error_type=None if daily_capture_complete else "missing_ixspy_daily_increment",
        error_message=None
        if daily_capture_complete
        else f"IXSPY 最近 7 日仅读取到 {len(daily_points)}/7 天日增量",
    )
    snapshot = save_external_snapshot(db, product, result, source, commit=False)
    imported_dates = (
        import_historical_sales(
            db,
            product.id,
            snapshot.id,
            daily_points,
            target_date=daily_target_date,
            commit=False,
        )
        if payload.historical_sales
        else set()
    )
    metrics = calculate_product_daily_metrics(
        db,
        product.id,
        timezone_name=get_settings().timezone,
        commit=False,
    )
    metric_dates = set(imported_dates)
    metric_dates.update(metric.metric_date for metric in metrics)
    for metric_date in metric_dates:
        calculate_store_daily_metric(
            db, product.store_id, metric_date, commit=False
        )
    evaluate_selection_candidate(db, product.id)
    if commit:
        db.commit()
        db.refresh(snapshot)
    return snapshot


def _save_store_observation(
    db: Session,
    payload: BrowserStoreDiscoveryRequest,
    *,
    commit: bool = True,
    expected_store_id: int | None = None,
    target_date: date | None = None,
    allow_pool_deactivation: bool = True,
) -> dict:
    normalized_store_url = normalize_aliexpress_url(payload.url, "store")
    url_store_id = extract_store_id(normalized_store_url)
    candidate_ids = {payload.platform_store_id}
    if url_store_id:
        candidate_ids.add(url_store_id)

    if expected_store_id is not None:
        store = db.get(Store, expected_store_id)
        if (
            store is not None
            and url_store_id
            and store.aliexpress_store_id
            and url_store_id != store.aliexpress_store_id
        ):
            raise HTTPException(status_code=409, detail="当前页面明确指向其他 AliExpress 店铺")
    else:
        matches = list(
            db.scalars(
                select(Store).where(Store.aliexpress_store_id.in_(candidate_ids))
            )
        )
        if len(matches) > 1:
            raise HTTPException(status_code=409, detail="店铺页面 ID 与采集 ID 不一致")
        store = matches[0] if matches else None
    if store is None:
        raise HTTPException(status_code=404, detail="该店铺尚未加入监控")
    if store.status != "active":
        raise HTTPException(status_code=409, detail="该店铺监控已停用")
    if expected_store_id is None and store.aliexpress_store_id not in candidate_ids:
        raise HTTPException(status_code=409, detail="当前店铺页与采集任务的监控店铺不一致")

    ranked_selection = payload.raw_data.get("collection_mode") == "top_7d_increment_selection"
    if ranked_selection:
        try:
            candidate_count = max(0, int(payload.raw_data.get("unique_candidate_count") or 0))
        except (TypeError, ValueError):
            candidate_count = 0
        expected_pool_size = min(20, candidate_count)
        selected_ids = [item.platform_product_id for item in payload.products]
        if (
            candidate_count == 0
            or len(set(selected_ids)) != len(selected_ids)
            or len(selected_ids) != expected_pool_size
        ):
            raise HTTPException(
                status_code=409,
                detail=f"店铺监控池应有 {expected_pool_size} 件商品，本次提交 {len(selected_ids)} 件；未修改现有监控池",
            )
        complete_history_count = 0
        for candidate in payload.products:
            expected_dates = candidate.history_expected_dates
            points = candidate.historical_sales
            point_dates = [point.date for point in points]
            if looks_cumulative_like_daily_values(
                [point.value for point in sorted(points, key=lambda point: point.date)]
            ):
                raise HTTPException(
                    status_code=409,
                    detail="七日增量曲线疑似累计销量，未修改监控池；请核对 IXSPY 增量图表",
                )
            complete = (
                candidate.history_daily_capture_complete
                and candidate.history_source == "ixspy_visible_daily_increment"
                and not candidate.history_missing_dates
                and len(expected_dates) == 7
                and len(set(expected_dates)) == 7
                and all(
                    expected_dates[index] - expected_dates[index - 1] == timedelta(days=1)
                    for index in range(1, len(expected_dates))
                )
                and len(points) == 7
                and len(set(point_dates)) == 7
                and set(point_dates) == set(expected_dates)
                and all(point.value_type == "daily_increment" for point in points)
            )
            complete_history_count += int(complete)
        if complete_history_count != expected_pool_size:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"七日增量筛选要求提交的 {expected_pool_size} 件商品均有完整 IXSPY 七日数据；"
                    f"当前 {complete_history_count} 件完整，未修改现有监控池"
                ),
            )
        for candidate in payload.products:
            normalized_product_url = normalize_aliexpress_url(candidate.url, "product")
            if extract_product_id(normalized_product_url) != candidate.platform_product_id:
                raise HTTPException(
                    status_code=409,
                    detail="七日增量筛选中商品 ID 与链接不一致，未修改监控池",
                )
            existing_product_id = db.scalar(
                select(Product.id).where(
                    Product.store_id == store.id,
                    Product.aliexpress_product_id == candidate.platform_product_id,
                )
            )
            conflicting_product_id = db.scalar(
                select(Product.id).where(Product.url == normalized_product_url)
            )
            if conflicting_product_id is not None and conflicting_product_id != existing_product_id:
                raise HTTPException(
                    status_code=409,
                    detail="七日增量筛选包含已归属其他店铺的商品链接，未修改监控池",
                )

    captured_at = datetime.now(timezone.utc)
    history_target_date = target_date or captured_at.astimezone(
        ZoneInfo(get_settings().timezone)
    ).date()
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
            if ranked_selection or product.discovery_source in ("store_page", "chrome_extension_store"):
                product.status = "active"
            product.discovery_rank = rank
            product.discovered_at = captured_at
            if candidate.title and not product.title:
                product.title = clean_product_title(candidate.title)
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
            title=clean_product_title(candidate.title),
            discovery_source="chrome_extension_store",
            discovery_rank=rank,
            discovered_at=captured_at,
        )
        db.add(product)
        db.flush()
        observed_products.append((product, candidate, rank))
        added_count += 1

    current_ids = {extract_product_id(link) for link in links}
    if current_ids and allow_pool_deactivation:
        pool_query = select(Product).where(Product.store_id == store.id, Product.status == "active")
        if not ranked_selection:
            pool_query = pool_query.where(
                Product.discovery_source.in_(("store_page", "chrome_extension_store"))
            )
        pool_products = list(db.scalars(pool_query))
        for product in pool_products:
            if product.aliexpress_product_id not in current_ids:
                product.status = "inactive"
                deactivated_count += 1

    store_snapshot = StoreSnapshot(
        store_id=store.id,
        collected_at=captured_at,
        collector_name="chrome_extension_store",
        collector_version=str(payload.raw_data.get("extractor_version", "1.0")),
        parse_status="success" if links else "failed",
        candidate_count=len(payload.products),
        source_http_status=200,
        raw_payload={
            "store_url": normalized_store_url,
            "submitted_platform_store_id": payload.platform_store_id,
            "resolved_platform_store_id": store.aliexpress_store_id,
            "store_id_resolution": (
                "task" if expected_store_id is not None
                else "platform_id" if payload.platform_store_id == store.aliexpress_store_id
                else "page_url"
            ),
            "products": [
                item.model_dump(mode="json", exclude={"public_cumulative_sold"})
                for item in payload.products
            ],
            "raw_data": payload.raw_data,
        },
        error_type=None if links else "no_valid_product_links",
        error_message=None if links else "没有读取到有效商品链接",
    )
    db.add(store_snapshot)
    db.flush()
    metric_date = captured_at.astimezone(ZoneInfo(get_settings().timezone)).date()
    metric_dates = {metric_date}
    for product, candidate, rank in observed_products:
        product_url = normalize_aliexpress_url(candidate.url, "product")
        product_snapshot = save_store_product_snapshot(
            db,
            product,
            captured_at=captured_at,
            sold_count=None,
            title=candidate.title,
            source="CHROME_EXTENSION",
            collector_name="chrome_extension_store",
            collector_version=str(payload.raw_data.get("extractor_version", "1.0")),
            raw_payload={
                "collection_mode": "store_page",
                "store_snapshot_id": store_snapshot.id,
                "rank": rank,
                "product_id": candidate.platform_product_id,
                "url": product_url,
                "title": candidate.title,
                "main_image_url": candidate.image_url,
                "extension_raw_data": payload.raw_data,
            },
            historical_sales=candidate.historical_sales,
            history_target_date=history_target_date,
            history_source=candidate.history_source,
            commit=False,
        )
        imported_dates = import_historical_sales(
            db,
            product.id,
            product_snapshot.id,
            candidate.historical_sales,
            target_date=history_target_date,
            commit=False,
        ) if candidate.history_source == "ixspy_visible_daily_increment" else set()
        metric_dates.update(imported_dates)
        calculate_product_daily_metrics(
            db,
            product.id,
            timezone_name=get_settings().timezone,
            commit=False,
        )
    if observed_products:
        for observed_date in metric_dates:
            calculate_store_daily_metric(db, store.id, observed_date, commit=False)
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
def create_store(payload: StoreCreate, db: Session = Depends(get_db)) -> dict:
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
    try:
        run, _queued_count, _reused_count, _discovery_queued = queue_store_collection(
            db, store, allow_paused=True
        )
        deferred = run.status in {"PAUSED", "NEEDS_VERIFICATION"}
        initial_collection_status = "deferred" if deferred else "queued"
        initial_collection_message = (
            "今日队列已暂停；店铺采集任务已排队，恢复队列后再启动"
            if run.status == "PAUSED"
            else "今日队列等待人工验证；店铺采集任务已排队，验证后继续"
            if run.status == "NEEDS_VERIFICATION"
            else None
        )
    except ValueError as error:
        initial_collection_status = "deferred"
        initial_collection_message = str(error)
    db.refresh(store)
    result = StoreRead.model_validate(store).model_dump()
    result["initial_collection_status"] = initial_collection_status
    result["initial_collection_message"] = initial_collection_message
    return result


@router.post("/stores/{store_id}/browser-collection")
def start_store_browser_collection(
    store_id: int, db: Session = Depends(get_db)
) -> dict:
    store = db.get(Store, store_id)
    if store is None:
        raise HTTPException(status_code=404, detail="Store not found")
    if store.status != "active":
        raise HTTPException(status_code=409, detail="该店铺监控已停用")
    try:
        run, queued_count, reused_count, discovery_queued = queue_store_collection(db, store)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {
        "run": serialize_run(run),
        "queued_count": queued_count,
        "reused_count": reused_count,
        "discovery_queued": discovery_queued,
    }


@router.post("/products/{product_id}/browser-collection")
def start_product_browser_collection(
    product_id: int, db: Session = Depends(get_db)
) -> dict:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    if product.status != "active":
        raise HTTPException(status_code=409, detail="该商品监控已停用")
    store = db.get(Store, product.store_id)
    if store is None or (
        store.status != "active" and product.monitor_scope != "selection_candidate"
    ):
        raise HTTPException(status_code=409, detail="所属店铺监控已停用")
    try:
        run, item, queued = queue_single_product_collection(db, product)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {
        "run": serialize_run(run),
        "item": serialize_item(item),
        "queued": queued,
    }


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
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    if payload.raw_data.get("collection_mode") == "top_7d_increment_selection":
        store = db.scalar(
            select(Store).where(Store.aliexpress_store_id == payload.platform_store_id)
        )
        if store is None:
            raise HTTPException(status_code=404, detail="该店铺尚未加入监控")
        try:
            return save_store_page_candidates(
                db,
                store,
                page_url=payload.url,
                candidates=payload.products,
                has_next_page=payload.raw_data.get("has_next_page"),
                next_page_url=payload.raw_data.get("next_page_url"),
                scan_complete=payload.raw_data.get("scan_complete") is True,
                source="CHROME_EXTENSION",
                raw_data=payload.raw_data,
            )
        except ValueError as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail="店铺页面商品已发生重复，未修改监控池") from exc
    if not payload.products:
        raise HTTPException(status_code=409, detail="店铺商品发现结果不能为空")
    return _save_store_observation(
        db,
        payload,
        allow_pool_deactivation=is_configured_admin_session(request, db),
    )


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


@router.post(
    "/integrations/selection/candidates",
    dependencies=[Depends(require_selection_service_token)],
    status_code=status.HTTP_201_CREATED,
)
def import_workbench_candidates(
    payload: SelectionCandidatesBatchImport,
    response: Response,
    db: Session = Depends(get_db),
) -> dict:
    try:
        result = import_selection_candidates(db, payload.batch_id, payload.candidates)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result["created_count"] == 0:
        response.status_code = status.HTTP_200_OK
    current_run = get_today_run(db)
    if current_run is not None:
        ensure_browser_collection_run(db, current_run.target_date, trigger="MANUAL")
    return result


@router.get(
    "/selection-qualification/rule",
    response_model=SelectionQualificationRuleRead,
)
def get_selection_qualification_rule(db: Session = Depends(get_db)) -> dict:
    rule = db.get(SelectionQualificationRule, 1)
    minimum_total = rule.minimum_7d_increment_total if rule else None
    minimum_positive_days = rule.minimum_positive_days if rule else None
    return {
        "minimum_7d_increment_total": minimum_total,
        "minimum_positive_days": minimum_positive_days,
        "configured": minimum_total is not None and minimum_positive_days is not None,
        "timezone": "Asia/Shanghai",
    }


@router.put(
    "/selection-qualification/rule",
    response_model=SelectionQualificationRuleRead,
)
def update_selection_qualification_rule(
    payload: SelectionQualificationRuleWrite,
    db: Session = Depends(get_db),
) -> dict:
    rule = set_selection_rule(
        db,
        payload.minimum_7d_increment_total,
        payload.minimum_positive_days,
    )
    evaluate_all_selection_candidates(db)
    db.commit()
    return {
        "minimum_7d_increment_total": rule.minimum_7d_increment_total,
        "minimum_positive_days": rule.minimum_positive_days,
        "configured": (
            rule.minimum_7d_increment_total is not None
            and rule.minimum_positive_days is not None
        ),
        "timezone": "Asia/Shanghai",
    }


@router.get("/selection-qualification/candidates")
def get_selection_candidates(db: Session = Depends(get_db)) -> list[dict]:
    candidates = [
        serialize_selection_candidate(candidate)
        for candidate in list_selection_candidates(db)
    ]
    run = get_today_run(db)
    collection_status_by_product = {
        item.product_id: item.status
        for item in (run.items if run else [])
        if item.target_type == "PRODUCT" and item.product_id is not None
    }
    for candidate in candidates:
        candidate["collection_status"] = collection_status_by_product.get(
            candidate["product_id"]
        )
    return candidates


@router.post("/selection-qualification/collect")
def queue_selection_candidate_collection(db: Session = Depends(get_db)) -> dict:
    existing_run = get_today_run(db)
    existing_product_ids = {
        item.product_id
        for item in (existing_run.items if existing_run else [])
        if item.target_type == "PRODUCT" and item.product_id is not None
    }
    run = ensure_browser_collection_run(db, trigger="MANUAL")
    candidate_products = list(
        db.scalars(
            select(Product)
            .join(SelectionCandidate, SelectionCandidate.product_id == Product.id)
            .where(
                Product.monitor_scope == "selection_candidate",
                Product.status == "active",
            )
        )
    )
    candidate_items = [
        item
        for item in run.items
        if item.target_type == "PRODUCT"
        and item.product_id in {product.id for product in candidate_products}
    ]
    return {
        "run_id": run.id,
        "candidate_count": len(candidate_products),
        "queued_count": sum(item.product_id not in existing_product_ids for item in candidate_items),
        "items": [
            {"product_id": item.product_id, "status": item.status, "target_url": item.target_url}
            for item in candidate_items
        ],
    }


@router.post("/selection-qualification/evaluate")
def reevaluate_selection_candidates(db: Session = Depends(get_db)) -> list[dict]:
    candidates = evaluate_all_selection_candidates(db)
    db.commit()
    candidates = [
        serialize_selection_candidate(candidate)
        for candidate in list_selection_candidates(db)
    ]
    run = get_today_run(db)
    collection_status_by_product = {
        item.product_id: item.status
        for item in (run.items if run else [])
        if item.target_type == "PRODUCT" and item.product_id is not None
    }
    for candidate in candidates:
        candidate["collection_status"] = collection_status_by_product.get(
            candidate["product_id"]
        )
    return candidates


@router.get("/integrations/dianxiaomi/status")
def dianxiaomi_handoff_status(
    request: Request, db: Session = Depends(get_db)
) -> dict:
    control = db.get(DianxiaomiQueueControl, 1)
    if control is None:
        control = DianxiaomiQueueControl(id=1)
        db.add(control)
        db.commit()
        db.refresh(control)
    status_counts = dict(
        db.execute(
            select(DianxiaomiHandoff.status, func.count(DianxiaomiHandoff.id))
            .group_by(DianxiaomiHandoff.status)
        ).all()
    )
    rows = list(
        db.scalars(
            select(DianxiaomiHandoff)
            .options(
                joinedload(DianxiaomiHandoff.product),
                joinedload(DianxiaomiHandoff.store),
            )
            .order_by(DianxiaomiHandoff.requested_at.desc(), DianxiaomiHandoff.id.desc())
            .limit(100)
        )
    )
    latest_active = db.scalar(
        select(DianxiaomiHandoff)
        .where(
            DianxiaomiHandoff.status.in_(
                ACTIVE_HANDOFF_STATUSES - {"QUEUED"}
            )
        )
        .order_by(DianxiaomiHandoff.requested_at, DianxiaomiHandoff.id)
        .limit(1)
    )
    now = datetime.now(timezone.utc)
    def count_for(status_value: str) -> int:
        return int(status_counts.get(status_value, 0))

    processing_count = sum(count_for(value) for value in ("CLAIMED", "OPENED", "FILLED"))
    queued_count = count_for("QUEUED")
    succeeded_count = count_for("SUCCEEDED")
    failed_count = count_for("FAILED")
    needs_confirmation_count = count_for("NEEDS_CONFIRMATION")
    completed_count = succeeded_count + failed_count + count_for("CANCELED")
    stale_cutoff = now - timedelta(minutes=10)
    stalled_count = 0
    if latest_active is not None:
        active_rows = list(
            db.scalars(
                select(DianxiaomiHandoff).where(
                    DianxiaomiHandoff.status.in_(ACTIVE_HANDOFF_STATUSES - {"QUEUED", "NEEDS_CONFIRMATION"}),
                    DianxiaomiHandoff.claimed_at.is_not(None),
                )
            )
        )
        stalled_count = sum(
            (row.claimed_at.replace(tzinfo=timezone.utc) if row.claimed_at.tzinfo is None else row.claimed_at) < stale_cutoff
            for row in active_rows
        )
    browser_control = db.get(BrowserCollectionControl, 1)
    selected_worker_id = browser_control.selected_worker_id if browser_control else None
    selected_worker_row = (
        db.get(BrowserCollectionWorker, selected_worker_id)
        if selected_worker_id else None
    )
    selected_heartbeat = selected_worker_row.last_heartbeat_at if selected_worker_row else None
    if selected_heartbeat and selected_heartbeat.tzinfo is None:
        selected_heartbeat = selected_heartbeat.replace(tzinfo=timezone.utc)
    selected_worker_online = bool(
        selected_heartbeat and now - selected_heartbeat <= timedelta(minutes=2)
    )
    selected_worker = (
        {
            "worker_id": selected_worker_id,
            "label": selected_worker_row.label if selected_worker_row else (
                browser_control.selected_worker_label if browser_control else selected_worker_id
            ),
            "online": selected_worker_online,
        }
        if selected_worker_id else None
    )
    active_owner = None
    if latest_active and latest_active.worker_id:
        owner_worker = db.get(BrowserCollectionWorker, latest_active.worker_id)
        active_owner = {
            "worker_id": latest_active.worker_id,
            "label": owner_worker.label if owner_worker else latest_active.worker_id,
        }
    latest_active_claimed_at = latest_active.claimed_at if latest_active else None
    if latest_active_claimed_at and latest_active_claimed_at.tzinfo is None:
        latest_active_claimed_at = latest_active_claimed_at.replace(tzinfo=timezone.utc)
    active_owner_holds_lease = bool(
        latest_active
        and latest_active.status in LEASED_HANDOFF_STATUSES
        and latest_active.worker_id
        and latest_active.worker_id != selected_worker_id
        and latest_active_claimed_at
        and latest_active_claimed_at >= stale_cutoff
    )
    waiting_reason = None
    if queued_count and not control.is_paused:
        if not selected_worker_id:
            waiting_reason = "未选择采集电脑"
        elif not selected_worker_online:
            waiting_reason = "选中的采集电脑离线，任务仍保留在队列中"
        elif active_owner_holds_lease:
            waiting_reason = "等待当前采集电脑完成在途任务后再交接"
    total_count = sum(status_counts.values())
    legacy_counts = {
        "queued": queued_count,
        "processing": processing_count + count_for("COLLECTING"),
        "submitted": count_for("COLLECTING"),
        "succeeded": succeeded_count,
        "failed": failed_count,
        "needs_confirmation": needs_confirmation_count,
    }
    return {
        **legacy_counts,
        "paused": control.is_paused,
        "paused_at": control.paused_at,
        "paused_reason": control.paused_reason,
        "worker_id": control.worker_id,
        "last_heartbeat_at": selected_heartbeat,
        "worker_online": selected_worker_online,
        "selected_worker": selected_worker,
        "active_owner": active_owner,
        "waiting_for_owner": active_owner_holds_lease and bool(queued_count),
        "waiting_reason": waiting_reason,
        "total_count": total_count,
        "queued_count": queued_count,
        "processing_count": processing_count,
        "submitted_count": count_for("COLLECTING"),
        "completed_count": completed_count,
        "succeeded_count": succeeded_count,
        "failed_count": failed_count,
        "needs_confirmation_count": needs_confirmation_count,
        "stalled_count": stalled_count,
        "can_delete_failed": is_configured_admin_session(request, db),
        "admin_delete_configured": bool(get_settings().auth_admin_username),
        "current_item": serialize_handoff(latest_active) if latest_active else None,
        "latest_at": rows[0].requested_at if rows else None,
        "items": [serialize_handoff(row) for row in rows],
        "latest": [serialize_handoff(row) for row in rows[:10]],
    }


@router.delete("/integrations/dianxiaomi/handoffs/failed")
def delete_failed_dianxiaomi_handoffs(
    db: Session = Depends(get_db),
    _admin=Depends(require_dianxiaomi_admin),
) -> dict:
    return {"deleted_count": delete_failed_handoffs(db)}


@router.delete("/integrations/dianxiaomi/handoffs/{handoff_id}")
def delete_failed_dianxiaomi_handoff(
    handoff_id: int,
    db: Session = Depends(get_db),
    _admin=Depends(require_dianxiaomi_admin),
) -> dict:
    handoff = db.scalar(
        select(DianxiaomiHandoff)
        .where(DianxiaomiHandoff.id == handoff_id)
        .with_for_update()
    )
    if handoff is None:
        raise HTTPException(status_code=404, detail="店小秘任务不存在")
    if handoff.status != "FAILED":
        raise HTTPException(status_code=409, detail="只能删除采集失败的店小秘任务")
    db.delete(handoff)
    db.commit()
    return {"deleted_count": 1}


@router.post("/integrations/dianxiaomi/queue/pause")
def pause_dianxiaomi_handoff_queue(db: Session = Depends(get_db)) -> dict:
    control = pause_dianxiaomi_queue(db, "manual_pause")
    return {
        "paused": control.is_paused,
        "paused_at": control.paused_at,
        "paused_reason": control.paused_reason,
    }


@router.post("/integrations/dianxiaomi/queue/resume")
def resume_dianxiaomi_handoff_queue(db: Session = Depends(get_db)) -> dict:
    control = resume_dianxiaomi_queue(db)
    return {
        "paused": control.is_paused,
        "paused_at": control.paused_at,
        "paused_reason": control.paused_reason,
    }


@router.post("/integrations/dianxiaomi/queue/heartbeat")
def dianxiaomi_worker_heartbeat(
    payload: DianxiaomiQueueHeartbeatRequest,
    db: Session = Depends(get_db),
) -> dict:
    control = record_dianxiaomi_heartbeat(db, payload.worker_id)
    return {
        "paused": control.is_paused,
        "paused_reason": control.paused_reason,
        "last_heartbeat_at": control.last_heartbeat_at,
        "worker_id": control.worker_id,
    }


@router.post(
    "/integrations/dianxiaomi/worker/heartbeat",
    dependencies=[Depends(require_dianxiaomi_service_token)],
)
def dianxiaomi_docker_worker_heartbeat(
    worker_id: str = Header(alias="X-Dianxiaomi-Worker-ID", min_length=1, max_length=128),
    db: Session = Depends(get_db),
) -> dict:
    control = record_dianxiaomi_heartbeat(db, worker_id)
    return {
        "paused": control.is_paused,
        "paused_reason": control.paused_reason,
        "last_heartbeat_at": control.last_heartbeat_at,
        "worker_id": control.worker_id,
    }


@router.post(
    "/integrations/dianxiaomi/worker/handoffs/claim",
    response_model=DianxiaomiWorkerHandoffRead | None,
    dependencies=[Depends(require_dianxiaomi_service_token)],
)
def claim_dianxiaomi_docker_handoff(
    worker_id: str = Header(alias="X-Dianxiaomi-Worker-ID", min_length=1, max_length=128),
    db: Session = Depends(get_db),
) -> dict | None:
    row = claim_next_handoff(db, worker_id)
    if row is None:
        return None
    return {
        "handoff_id": row.id,
        "batch_id": row.batch_id,
        "aliexpress_product_id": row.product.aliexpress_product_id,
        "url": row.target_url,
        "attempt_count": row.attempt_count,
        "status": row.status,
        "detail": row.detail_json,
    }


@router.post(
    "/integrations/dianxiaomi/worker/handoffs/{handoff_id}/status",
    response_model=DianxiaomiWorkerHandoffRead,
    dependencies=[Depends(require_dianxiaomi_service_token)],
)
def update_dianxiaomi_docker_handoff(
    handoff_id: int,
    payload: DianxiaomiWorkerStatusRequest,
    worker_id: str = Header(alias="X-Dianxiaomi-Worker-ID", min_length=1, max_length=128),
    db: Session = Depends(get_db),
) -> dict:
    row = _handoff_or_404(db, handoff_id)
    if row.worker_id != worker_id:
        raise HTTPException(status_code=409, detail="店小秘任务不属于当前服务实例")
    if row.status not in ACTIVE_HANDOFF_STATUSES:
        raise HTTPException(status_code=409, detail="该店小秘任务已结束")
    detail = payload.detail
    updated = update_handoff(
        db,
        row,
        payload.status,
        error_type=(
            detail.get("error_type") if isinstance(detail.get("error_type"), str) else None
        ),
        error_message=(
            detail.get("error_message")
            if isinstance(detail.get("error_message"), str)
            else None
        ),
        detail=detail,
        worker_id=worker_id,
    )
    return {
        "handoff_id": updated.id,
        "batch_id": updated.batch_id,
        "aliexpress_product_id": updated.product.aliexpress_product_id,
        "url": updated.target_url,
        "attempt_count": updated.attempt_count,
        "status": updated.status,
        "detail": updated.detail_json,
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
    _handoff_or_404(db, handoff_id)
    row = claim_handoff(db, handoff_id, payload.worker_id)
    if row is None:
        raise HTTPException(
            status_code=409,
            detail="任务未领取：请确认当前采集电脑已选中且在线、队列未暂停，且没有其他电脑的有效任务",
        )
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
    if not payload.worker_id or row.worker_id != payload.worker_id:
        raise HTTPException(status_code=409, detail="店小秘任务不属于当前扩展实例")
    return serialize_handoff(
        update_handoff(
            db,
            row,
            payload.status,
            error_type=payload.error_type,
            error_message=payload.error_message,
            detail=payload.detail,
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
    try:
        return serialize_handoff(retry_handoff(db, row))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/integrations/dianxiaomi/handoffs/{handoff_id}/identity-review/confirm",
    response_model=DianxiaomiHandoffRead,
)
def confirm_dianxiaomi_product_page_identity(
    handoff_id: int,
    db: Session = Depends(get_db),
) -> dict:
    row = _handoff_or_404(db, handoff_id)
    try:
        return serialize_handoff(confirm_product_page_identity(db, row))
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/integrations/dianxiaomi/handoffs/{handoff_id}/identity-review/reject",
    response_model=DianxiaomiHandoffRead,
)
def reject_dianxiaomi_product_page_identity(
    handoff_id: int,
    db: Session = Depends(get_db),
) -> dict:
    row = _handoff_or_404(db, handoff_id)
    try:
        return serialize_handoff(reject_product_page_identity(db, row))
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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


def _claimed_browser_collection_item(
    db: Session,
    item_id: int,
    payload: BrowserExtensionCollectionRequest | BrowserStoreDiscoveryRequest
    | BrowserCollectionFailureRequest | BrowserCollectionChallengeRequest
    | BrowserCollectionIdentityReviewRequest,
    target_type: str | None = None,
) -> BrowserCollectionItem:
    item = _browser_collection_item(db, item_id, target_type)
    try:
        return validate_browser_collection_claim(
            db, item.id, payload.worker_id, payload.claim_token
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get(
    "/browser-collection/control",
    response_model=BrowserCollectionControlRead,
)
def browser_collection_control(db: Session = Depends(get_db)) -> dict:
    return browser_collection_control_status(db)


@router.post(
    "/browser-collection/pool-resets",
    response_model=StorePoolResetBatchRead,
)
def browser_collection_create_pool_reset(
    payload: StorePoolResetCreateRequest,
    _admin=Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    try:
        batch = create_pool_reset_batch(db, confirm_replace=payload.confirm_replace)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return serialize_pool_reset_batch(batch)


@router.get(
    "/browser-collection/pool-resets/latest",
    response_model=StorePoolResetBatchRead | None,
)
def browser_collection_latest_pool_reset(db: Session = Depends(get_db)) -> dict | None:
    batch = get_latest_pool_reset_batch(db)
    return serialize_pool_reset_batch(batch) if batch else None


@router.post(
    "/browser-collection/pool-resets/{batch_id}/start",
    response_model=StorePoolResetBatchRead,
)
def browser_collection_start_pool_reset(
    batch_id: int,
    _admin=Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    try:
        return serialize_pool_reset_batch(start_pool_reset_batch(db, batch_id))
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post(
    "/browser-collection/pool-resets/{batch_id}/cancel",
    response_model=StorePoolResetBatchRead,
)
def browser_collection_cancel_pool_reset(
    batch_id: int,
    _admin=Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    try:
        return serialize_pool_reset_batch(cancel_pool_reset_batch(db, batch_id))
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post(
    "/browser-collection/pool-resets/claim",
    response_model=StorePoolResetClaimRead,
)
def browser_collection_claim_pool_reset(
    payload: StorePoolResetClaimRequest,
    db: Session = Depends(get_db),
) -> dict:
    try:
        claimed = claim_pool_reset_store(db, payload.worker_id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if claimed is None:
        batch = get_latest_pool_reset_batch(db)
        return {
            "batch": serialize_pool_reset_batch(batch) if batch else None,
            "store": None,
            "claim_token": None,
            "candidate_limit": 0,
            "target_url": None,
            "target_date": batch.target_date if batch else None,
        }
    batch, task, token, candidate_limit = claimed
    serialized = serialize_pool_reset_batch(batch)
    task_data = next(item for item in serialized["stores"] if item["id"] == task.id)
    return {
        "batch": serialized,
        "store": task_data,
        "claim_token": token,
        "candidate_limit": candidate_limit,
        "target_url": task.claimed_page_url,
        "target_date": batch.target_date,
    }


@router.post("/browser-collection/pool-resets/heartbeat")
def browser_collection_pool_reset_heartbeat(
    payload: StorePoolResetHeartbeatRequest,
    db: Session = Depends(get_db),
) -> dict:
    try:
        return record_pool_reset_heartbeat(
            db,
            payload.worker_id,
            payload.claim_token,
            worker_label=payload.worker_label,
            extension_version=payload.extension_version,
            capabilities=payload.capabilities,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post(
    "/browser-collection/pool-resets/tasks/{task_id}/page",
    response_model=StorePoolResetBatchRead,
)
def browser_collection_pool_reset_page(
    task_id: int,
    payload: StorePoolResetPageRequest,
    db: Session = Depends(get_db),
) -> dict:
    try:
        batch = record_pool_reset_page(
            db,
            task_id,
            payload.worker_id,
            payload.claim_token,
            payload.expected_store_id,
            payload.page,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return serialize_pool_reset_batch(batch)


@router.post(
    "/browser-collection/pool-resets/tasks/{task_id}/failure",
    response_model=StorePoolResetBatchRead,
)
def browser_collection_pool_reset_failure(
    task_id: int,
    payload: StorePoolResetFailureRequest,
    db: Session = Depends(get_db),
) -> dict:
    try:
        batch = record_pool_reset_failure(
            db,
            task_id,
            payload.worker_id,
            payload.claim_token,
            payload.error_type,
            payload.error_message,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return serialize_pool_reset_batch(batch)


@router.post(
    "/browser-collection/pool-resets/tasks/{task_id}/challenge",
    response_model=StorePoolResetBatchRead,
)
def browser_collection_pool_reset_challenge(
    task_id: int,
    payload: StorePoolResetChallengeRequest,
    db: Session = Depends(get_db),
) -> dict:
    try:
        batch = record_pool_reset_challenge(
            db,
            task_id,
            payload.worker_id,
            payload.claim_token,
            payload.error_message,
            payload.page_url,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return serialize_pool_reset_batch(batch)


@router.post(
    "/browser-collection/pool-resets/tasks/{task_id}/resume",
    response_model=StorePoolResetBatchRead,
)
def browser_collection_pool_reset_resume(
    task_id: int,
    payload: StorePoolResetResumeRequest,
    db: Session = Depends(get_db),
) -> dict:
    try:
        batch = resume_pool_reset_after_challenge(
            db,
            task_id,
            payload.worker_id,
            payload.claim_token,
            payload.page_url,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return serialize_pool_reset_batch(batch)


@router.post(
    "/browser-collection/pool-resets/tasks/{task_id}/retry",
    response_model=StorePoolResetBatchRead,
)
def browser_collection_pool_reset_retry(
    task_id: int,
    _admin=Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    try:
        batch = retry_pool_reset_store(db, task_id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return serialize_pool_reset_batch(batch)


@router.post("/browser-collection/control/worker", response_model=BrowserCollectionControlRead)
def browser_collection_select_worker(
    payload: BrowserCollectionWorkerSelectRequest,
    db: Session = Depends(get_db),
) -> dict:
    try:
        return select_browser_collection_worker(
            db, payload.worker_id, confirm_handoff=payload.confirm_handoff
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post(
    "/browser-collection/runs/start-today",
    response_model=BrowserCollectionRunRead,
)
def browser_collection_start_today(
    payload: BrowserCollectionRunStartRequest | None = None,
    db: Session = Depends(get_db),
) -> dict:
    try:
        return serialize_run(
            start_browser_collection_run(
                db,
                helper_worker_ids=payload.helper_worker_ids if payload else None,
            )
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


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


@router.post("/browser-collection/runs/collect-all")
def browser_collection_collect_all(db: Session = Depends(get_db)) -> dict:
    try:
        run, queued_count, reused_count, active_store_count = queue_all_store_collections(db)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {
        "run": serialize_run(run),
        "queued_count": queued_count,
        "reused_count": reused_count,
        "active_store_count": active_store_count,
    }


@router.post(
    "/browser-collection/items/claim",
    response_model=BrowserCollectionClaimRead,
)
def browser_collection_claim(
    payload: BrowserCollectionClaimRequest | None = None,
    db: Session = Depends(get_db),
) -> dict:
    run, item = claim_next_item(db, payload.worker_id if payload else None)
    return {
        "run": serialize_run(run) if run else None,
        "item": serialize_item(item, include_targets=True) if item else None,
        "claim_token": item.claim_token if item else None,
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
    item = _claimed_browser_collection_item(db, item_id, payload, "PRODUCT")
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
                "history_target_date": item.run.target_date.isoformat(),
            }
        }
    )
    snapshot = _save_channel_observation(
        db,
        scheduled_payload,
        "CHROME_EXTENSION",
        commit=False,
        expected_product=product,
        daily_target_date=item.run.target_date,
    )
    if snapshot.product_id != item.product_id:
        db.rollback()
        raise HTTPException(status_code=409, detail="Snapshot product does not match task")
    raw_snapshot = snapshot.raw_data if isinstance(snapshot.raw_data, dict) else {}
    daily_capture_complete = raw_snapshot.get("history_daily_capture_complete") is True
    run = finish_item(
        db,
        item,
        "SUCCEEDED" if snapshot.status == "VALID" and daily_capture_complete else "PARTIAL",
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
    item = _claimed_browser_collection_item(db, item_id, payload, "STORE")
    try:
        snapshot, complete, diagnostics = save_monitored_store_observation(db, item, payload)
    except StoreIdentityValidationError as exc:
        db.rollback()
        retry_item = db.get(BrowserCollectionItem, item_id)
        if retry_item is None:
            raise HTTPException(status_code=409, detail="Browser collection item not found")
        try:
            run = record_store_identity_review(
                db,
                retry_item,
                exc.page_url,
                exc.identity_error_type,
                exc.message,
                exc.evidence,
                worker_id=payload.worker_id or "",
                claim_token=payload.claim_token or "",
            )
        except ValueError as review_error:
            db.rollback()
            raise HTTPException(status_code=409, detail=str(review_error)) from review_error
        return serialize_run(run)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    candidate_status = diagnostics.get("candidate_scan", {}).get("status")
    partial_reasons = []
    if diagnostics["missing_dates"]:
        partial_reasons.append(f"待补 {len(diagnostics['missing_dates'])} 件商品的七日日增量")
    if candidate_status in {"incomplete", "rejected", "stale"}:
        partial_reasons.append("候选商品页扫描未完成，游标未推进")
    run = finish_item(
        db, item, "SUCCEEDED" if complete else "PARTIAL",
        error_type=None if complete else "ixspy_store_page_pending",
        error_message=None if complete else "；".join(partial_reasons),
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
    item = _claimed_browser_collection_item(db, item_id, payload)
    return serialize_run(
        fail_item(db, item, payload.error_type, payload.error_message)
    )


@router.post(
    "/browser-collection/items/{item_id}/retry",
    response_model=BrowserCollectionRunRead,
)
def browser_collection_retry(item_id: int, db: Session = Depends(get_db)) -> dict:
    item = db.get(BrowserCollectionItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Browser collection item not found")
    try:
        return serialize_run(retry_item(db, item))
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post(
    "/browser-collection/items/{item_id}/challenge",
    response_model=BrowserCollectionRunRead,
)
def browser_collection_challenge(
    item_id: int,
    payload: BrowserCollectionChallengeRequest,
    db: Session = Depends(get_db),
) -> dict:
    item = _claimed_browser_collection_item(db, item_id, payload)
    return serialize_run(pause_for_challenge(db, item, payload.error_message))


@router.post(
    "/browser-collection/items/{item_id}/identity-review",
    response_model=BrowserCollectionRunRead,
)
def browser_collection_identity_review(
    item_id: int,
    payload: BrowserCollectionIdentityReviewRequest,
    db: Session = Depends(get_db),
) -> dict:
    item = _browser_collection_item(db, item_id, "STORE")
    try:
        run = record_store_identity_review(
            db,
            item,
            payload.page_url,
            payload.identity_error_type,
            payload.error_message,
            payload.evidence.model_dump(),
            worker_id=payload.worker_id,
            claim_token=payload.claim_token,
        )
    except ValueError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(error)) from error
    return serialize_run(run)


@router.post(
    "/browser-collection/items/{item_id}/identity-review/confirm",
    response_model=BrowserCollectionRunRead,
)
def browser_collection_identity_review_confirm(
    item_id: int,
    db: Session = Depends(get_db),
) -> dict:
    try:
        return serialize_run(confirm_store_identity_review(db, item_id))
    except ValueError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post(
    "/browser-collection/items/{item_id}/identity-review/reject",
    response_model=BrowserCollectionRunRead,
)
def browser_collection_identity_review_reject(
    item_id: int,
    db: Session = Depends(get_db),
) -> dict:
    try:
        return serialize_run(reject_store_identity_review(db, item_id))
    except ValueError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post(
    "/browser-collection/runs/{run_id}/resume",
    response_model=BrowserCollectionRunRead,
)
def browser_collection_resume(
    run_id: int,
    payload: BrowserCollectionRunResumeRequest | None = None,
    db: Session = Depends(get_db),
) -> dict:
    run = db.get(BrowserCollectionRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Browser collection run not found")
    if run.status not in {"NEEDS_VERIFICATION", "PAUSED"}:
        raise HTTPException(status_code=409, detail="Browser collection run is not paused")
    try:
        return serialize_run(
            resume_run(
                db,
                run,
                payload.helper_worker_ids if payload else None,
            )
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post(
    "/browser-collection/runs/{run_id}/pause",
    response_model=BrowserCollectionRunRead,
)
def browser_collection_pause(
    run_id: int,
    db: Session = Depends(get_db),
) -> dict:
    run = db.get(BrowserCollectionRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Browser collection run not found")
    try:
        return serialize_run(pause_run(db, run))
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post(
    "/browser-collection/runs/{run_id}/cancel",
    response_model=BrowserCollectionRunRead,
)
def browser_collection_cancel(
    run_id: int,
    _admin=Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    run = db.get(BrowserCollectionRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Browser collection run not found")
    try:
        return serialize_run(cancel_run(db, run))
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post(
    "/browser-collection/heartbeat",
    response_model=BrowserCollectionRunRead,
)
def browser_collection_heartbeat(
    payload: BrowserCollectionHeartbeatRequest,
    db: Session = Depends(get_db),
) -> dict:
    try:
        run = record_heartbeat(
            db,
            [(claim.item_id, claim.claim_token) for claim in payload.active_claims],
            worker_id=payload.worker_id,
            worker_label=payload.worker_label,
            extension_version=payload.extension_version,
            capabilities=payload.capabilities,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return serialize_run(run)


@router.patch("/stores/{store_id}", response_model=StoreRead)
def update_store(
    store_id: int,
    payload: StoreUpdate,
    request: Request,
    db: Session = Depends(get_db),
) -> Store:
    store = db.get(Store, store_id)
    if store is None:
        raise HTTPException(status_code=404, detail="Store not found")
    updates = payload.model_dump(exclude_unset=True)
    if updates.get("status") == "inactive" and not is_configured_admin_session(request, db):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="只有管理员可以停用监控店铺")
    if "url" in updates:
        updates["url"] = normalize_aliexpress_url(updates["url"], "store")
    for field, value in updates.items():
        setattr(store, field, value.strip() if isinstance(value, str) else value)
    commit_or_conflict(db, "Store URL or AliExpress store ID already exists")
    db.refresh(store)
    return store


@router.post("/stores/{store_id}/deactivate", response_model=StoreRead)
def deactivate_store(
    store_id: int,
    request: Request,
    _admin=Depends(require_admin),
    db: Session = Depends(get_db),
) -> Store:
    return update_store(store_id, StoreUpdate(status="inactive"), request, db)


@router.post(
    "/stores/{store_id}/discover",
    response_model=StoreDiscoverySummaryRead,
)
def discover_store(
    store_id: int,
    request: Request,
    limit: int = Query(default=20, ge=1, le=500),
    db: Session = Depends(get_db),
    collector: StoreDiscoveryCollector = Depends(get_store_discovery_collector),
) -> dict:
    store = db.get(Store, store_id)
    if store is None:
        raise HTTPException(status_code=404, detail="Store not found")
    if store.status != "active":
        raise HTTPException(status_code=409, detail="Store monitoring is inactive")
    return discover_store_products(
        db,
        store,
        collector,
        limit,
        allow_deactivation=is_configured_admin_session(request, db),
    ).to_dict()


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
    target_date = metric_date or (collection_window_end_date() - timedelta(days=1))
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
        title=clean_product_title(payload.title),
    )
    db.add(product)
    commit_or_conflict(db, "Product URL or AliExpress product ID already exists for this store")
    db.refresh(product)
    return product


@router.get("/products", response_model=list[ProductRead])
def list_products(
    store_id: int | None = None,
    status_filter: str | None = Query(default=None, alias="status", pattern="^(active|inactive)$"),
    scope: str = Query(default="competitor", pattern="^(competitor|selection_candidate|all)$"),
    db: Session = Depends(get_db),
) -> list[Product]:
    statement = select(Product).order_by(Product.id)
    if scope != "all":
        statement = statement.where(Product.monitor_scope == scope)
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
    product_id: int,
    payload: ProductUpdate,
    request: Request,
    db: Session = Depends(get_db),
) -> Product:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    values = payload.model_dump(exclude_unset=True)
    if values.get("status") == "inactive" and not is_configured_admin_session(request, db):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="只有管理员可以停用商品监控")
    for field, value in values.items():
        setattr(product, field, value)
    if "status" in values:
        product.pool_reset_reactivation_eligible = False
    db.commit()
    db.refresh(product)
    return product


@router.post("/products/{product_id}/deactivate", response_model=ProductRead)
def deactivate_product(
    product_id: int,
    request: Request,
    _admin=Depends(require_admin),
    db: Session = Depends(get_db),
) -> Product:
    return update_product(product_id, ProductUpdate(status="inactive"), request, db)


@router.post(
    "/products/clear-active",
    response_model=ProductMonitorClearResponse,
)
def clear_active_monitored_products(
    payload: ProductMonitorClearRequest,
    _admin=Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    active_store_ids = select(Store.id).where(Store.status == "active")
    result = db.execute(
        update(Product)
        .where(
            Product.status == "active",
            Product.store_id.in_(active_store_ids),
        )
        .values(status="inactive", pool_reset_reactivation_eligible=True)
    )
    db.commit()
    return {"deactivated_count": max(result.rowcount or 0, 0)}


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
    for metric in metrics:
        calculate_store_daily_metric(db, product.store_id, metric.metric_date)
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
        .where(
            ProductDailyMetric.product_id == product_id,
            ProductDailyMetric.calculation_method == "ixspy_visible_daily_increment",
        )
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
        .where(
            StoreDailyMetric.store_id == store_id,
            StoreDailyMetric.calculation_method == "sum_of_ixspy_visible_daily_increments",
            StoreDailyMetric.estimable_products > 0,
        )
        .order_by(StoreDailyMetric.metric_date.desc())
        .limit(limit)
    )
    return list(db.scalars(statement))


@router.post("/jobs/collect-now")
def collect_all_now(
    db: Session = Depends(get_db), collector: Collector = Depends(get_collector)
) -> dict:
    return run_collection_cycle(db, collector).to_dict()
