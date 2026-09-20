from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.schemas import (
    BrowserExtensionCollectionRequest,
    BrowserStoreDiscoveryRead,
    BrowserStoreDiscoveryRequest,
    CollectionAttemptRead,
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
from app.core.config import get_settings
from app.api.utils import (
    canonical_product_url,
    extract_product_id,
    extract_store_id,
    normalize_aliexpress_url,
)
from app.db.models import (
    CollectionAttempt,
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
from app.services.snapshots import save_external_snapshot
from app.services.collection_runs import run_collection_cycle
from app.services.metrics import calculate_product_daily_metrics, calculate_store_daily_metric
from app.services.store_discovery import discover_store_products, store_top_products

router = APIRouter(prefix="/api")


def _find_extension_product(db: Session, payload: BrowserExtensionCollectionRequest) -> Product:
    normalized_url = normalize_aliexpress_url(payload.url, "product")
    url_product_id = extract_product_id(normalized_url)
    if url_product_id != payload.platform_product_id:
        raise HTTPException(status_code=422, detail="商品 ID 与商品链接不匹配")
    products = list(
        db.scalars(
            select(Product)
            .where(Product.aliexpress_product_id == payload.platform_product_id)
            .limit(2)
        )
    )
    if not products:
        raise HTTPException(status_code=404, detail="该商品尚未加入监控")
    if len(products) > 1:
        raise HTTPException(status_code=409, detail="该商品 ID 对应多个监控商品，请使用手动输入渠道")
    return products[0]


def _save_channel_observation(
    db: Session, payload: BrowserExtensionCollectionRequest, source: str
) -> ProductSnapshot:
    product = _find_extension_product(db, payload)
    captured_at = payload.captured_at or datetime.now(ZoneInfo(get_settings().timezone))
    parse_status = "success" if payload.sold_count is not None else "partial"
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
        raw_payload=payload.raw_data,
        raw_content=None,
        error_type=None if payload.sold_count is not None else "missing_sold_count",
        error_message=None if payload.sold_count is not None else "公开页面未提供累计销量",
    )
    return save_external_snapshot(db, product, result, source)


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
    skipped_count = 0
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
            product.status = "active"
            product.discovery_rank = rank
            product.discovered_at = captured_at
            if candidate.title and not product.title:
                product.title = candidate.title
            existing_count += 1
            continue
        conflicting = db.scalar(select(Product).where(Product.url == normalized_url))
        if conflicting is not None:
            skipped_count += 1
            continue
        db.add(
            Product(
                store_id=store.id,
                aliexpress_product_id=candidate.platform_product_id,
                url=normalized_url,
                title=candidate.title,
                discovery_source="chrome_extension_store",
                discovery_rank=rank,
                discovered_at=captured_at,
            )
        )
        added_count += 1

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
    db.commit()
    return {
        "store_id": store.id,
        "discovered_count": len(payload.products),
        "added_count": added_count,
        "existing_count": existing_count,
        "skipped_count": skipped_count,
        "product_links": links,
        "status": "success" if links else "failed",
        "error_message": None if links else "没有读取到有效商品链接",
    }


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
    manual_completed = {
        task.product_id
        for task in db.scalars(
            select(ManualCollectionTask).where(
                ManualCollectionTask.status == "COMPLETED",
                ManualCollectionTask.completed_at >= datetime.combine(today, datetime.min.time(), tzinfo=ZoneInfo(get_settings().timezone)),
            )
        )
    }
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
        store_id=payload.store_id,
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
