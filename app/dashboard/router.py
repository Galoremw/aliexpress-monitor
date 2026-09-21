from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

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
from app.core.config import get_settings
from app.services.store_discovery import store_top_products
from app.services.collection_progress import (
    build_collection_progress,
    manual_completed_product_ids,
    snapshot_collection_state,
)
from app.services.browser_collection import get_today_run, serialize_run

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory="app/templates")


def _collection_status(db: Session) -> dict:
    today = datetime.now(ZoneInfo(get_settings().timezone)).date()
    day_start = datetime.combine(today, datetime.min.time(), tzinfo=ZoneInfo(get_settings().timezone))
    active_products = list(
        db.scalars(
            select(Product)
            .join(Store, Store.id == Product.store_id)
            .where(Product.status == "active", Store.status == "active")
        )
    )
    attempts = list(
        db.scalars(select(CollectionAttempt).where(CollectionAttempt.attempted_at >= day_start))
    )
    auto_success = {a.product_id for a in attempts if a.source == "AUTO" and a.status != "FAILED"}
    auto_failed = {a.product_id for a in attempts if a.source == "AUTO" and a.status == "FAILED"}
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


def _snapshot_status(snapshot: ProductSnapshot | None) -> tuple[str, str]:
    if snapshot is None:
        return "pending", "待采集"
    if snapshot.parse_status == "success":
        return "success", "采集成功"
    if snapshot.parse_status == "partial":
        return "partial", "部分数据"
    raw = (snapshot.raw_content or "").lower()
    if snapshot.error_type == "platform_challenge" or (
        snapshot.error_type == "page_structure_unrecognized"
        and "captcha" in raw
        and "/punish?" in raw
    ):
        return "failed", "平台验证页"
    return "failed", "采集失败"


def _product_monitor_rows(
    db: Session, products: list[Product], history_dates: list[date]
) -> list[dict]:
    product_ids = [product.id for product in products]
    if not product_ids:
        return []
    metrics = list(
        db.scalars(
            select(ProductDailyMetric).where(
                ProductDailyMetric.product_id.in_(product_ids),
                ProductDailyMetric.metric_date.in_(history_dates),
            )
        )
    )
    metric_by_key = {(metric.product_id, metric.metric_date): metric for metric in metrics}
    latest_snapshot_by_product: dict[int, ProductSnapshot] = {}
    for snapshot in db.scalars(
        select(ProductSnapshot)
        .where(ProductSnapshot.product_id.in_(product_ids))
        .order_by(ProductSnapshot.collected_at.desc(), ProductSnapshot.id.desc())
    ):
        latest_snapshot_by_product.setdefault(snapshot.product_id, snapshot)

    rows = []
    latest_date = history_dates[-1]
    for product in products:
        history = []
        for metric_date in history_dates:
            metric = metric_by_key.get((product.id, metric_date))
            history.append(
                {
                    "date": metric_date,
                    "value": metric.estimated_sales if metric and metric.is_estimable else None,
                    "reason": metric.reason if metric else "no_snapshot",
                }
            )
        estimable_values = [item["value"] for item in history if item["value"] is not None]
        latest_metric = metric_by_key.get((product.id, latest_date))
        latest_snapshot = latest_snapshot_by_product.get(product.id)
        status_class, status_label = _snapshot_status(latest_snapshot)
        collection_state, collection_label, collection_status_class, collection_captured_at = snapshot_collection_state(
            latest_snapshot,
            latest_date + timedelta(days=1),
            ZoneInfo(get_settings().timezone),
        )
        rows.append(
            {
                "product": product,
                "latest_metric": latest_metric,
                "history": history,
                "seven_day_total": sum(estimable_values) if estimable_values else None,
                "coverage": len(estimable_values),
                "latest_snapshot": latest_snapshot,
                "status_class": status_class,
                "status_label": status_label,
                "collection_state": collection_state,
                "collection_status_class": collection_status_class,
                "collection_status_label": collection_label,
                "collection_captured_at": collection_captured_at
                if collection_state in {"completed", "failed"}
                else None,
            }
        )
    return rows


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    stores = list(db.scalars(select(Store).order_by(Store.status, Store.name)))
    all_products = list(
        db.scalars(select(Product).order_by(Product.status, Product.id.desc()))
    )
    products = [
        product for product in all_products if product.status == "active"
    ]
    recent_snapshots = list(
        db.scalars(
            select(ProductSnapshot)
            .order_by(ProductSnapshot.collected_at.desc(), ProductSnapshot.id.desc())
            .limit(10)
        )
    )
    recent_snapshot_rows = []
    for snapshot in recent_snapshots:
        status_class, status_label = _snapshot_status(snapshot)
        recent_snapshot_rows.append(
            {
                "snapshot": snapshot,
                "status_class": status_class,
                "status_label": status_label,
            }
        )
    latest_store_metrics: list[StoreDailyMetric] = []
    seen_stores: set[int] = set()
    for metric in db.scalars(
        select(StoreDailyMetric).order_by(
            StoreDailyMetric.metric_date.desc(), StoreDailyMetric.id.desc()
        )
    ):
        if metric.store_id not in seen_stores:
            seen_stores.add(metric.store_id)
            latest_store_metrics.append(metric)
    growth_metrics = list(
        db.execute(
            select(ProductDailyMetric, Product)
            .join(Product, Product.id == ProductDailyMetric.product_id)
            .where(ProductDailyMetric.is_estimable.is_(True), Product.status == "active")
            .order_by(
                ProductDailyMetric.metric_date.desc(),
                ProductDailyMetric.estimated_sales.desc(),
            )
            .limit(20)
        ).all()
    )
    today = datetime.now(ZoneInfo(get_settings().timezone)).date()
    history_dates = [today - timedelta(days=offset) for offset in range(7, 0, -1)]
    monitor_rows = _product_monitor_rows(db, products, history_dates)
    rows_by_store: dict[int, list[dict]] = {store.id: [] for store in stores}
    for row in monitor_rows:
        rows_by_store[row["product"].store_id].append(row)
    latest_metric_by_store = {metric.store_id: metric for metric in latest_store_metrics}
    store_panels = [
        {
            "store": store,
            "rows": rows_by_store[store.id],
            "latest_metric": latest_metric_by_store.get(store.id),
        }
        for store in stores
        if store.status == "active"
    ]
    browser_collection_run = get_today_run(db)
    context = {
        "request": request,
        "stores": stores,
        "products": products,
        "recent_snapshots": recent_snapshots,
        "recent_snapshot_rows": recent_snapshot_rows,
        "latest_store_metrics": latest_store_metrics,
        "growth_metrics": growth_metrics,
        "store_by_id": {store.id: store for store in stores},
        "product_by_id": {product.id: product for product in all_products},
        "history_dates": history_dates,
        "store_panels": store_panels,
        "collection_status": _collection_status(db),
        "collection_progress": build_collection_progress(db),
        "browser_collection_run": serialize_run(browser_collection_run)
        if browser_collection_run
        else None,
        "active_store_count": db.scalar(
            select(func.count()).select_from(Store).where(Store.status == "active")
        ),
        "active_product_count": db.scalar(
            select(func.count()).select_from(Product).where(Product.status == "active")
        ),
        "failed_snapshot_count": db.scalar(
            select(func.count())
            .select_from(ProductSnapshot)
            .where(ProductSnapshot.parse_status == "failed")
        ),
    }
    return templates.TemplateResponse(request, "dashboard.html", context)


@router.get("/dashboard/products/{product_id}", response_class=HTMLResponse)
def product_detail(
    product_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    snapshots = list(
        db.scalars(
            select(ProductSnapshot)
            .where(ProductSnapshot.product_id == product_id)
            .order_by(ProductSnapshot.collected_at.desc(), ProductSnapshot.id.desc())
            .limit(100)
        )
    )
    metrics = list(
        db.scalars(
            select(ProductDailyMetric)
            .where(ProductDailyMetric.product_id == product_id)
            .order_by(ProductDailyMetric.metric_date.desc())
            .limit(90)
        )
    )
    return templates.TemplateResponse(
        request,
        "product_detail.html",
        {
            "request": request,
            "product": product,
            "store": product.store,
            "snapshots": snapshots,
            "metrics": metrics,
            "max_estimate": max(
                (metric.estimated_sales or 0 for metric in metrics if metric.is_estimable),
                default=0,
            ),
        },
    )


@router.get("/collection/pending", response_class=HTMLResponse)
@router.get("/manual", response_class=HTMLResponse)
def pending_collection(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    tasks = list(
        db.scalars(
            select(ManualCollectionTask)
            .join(Product, Product.id == ManualCollectionTask.product_id)
            .join(Store, Store.id == Product.store_id)
            .where(ManualCollectionTask.status == "PENDING", Product.status == "active")
            .order_by(ManualCollectionTask.created_at.desc())
        )
    )
    rows = []
    for task in tasks:
        product = task.product
        latest_success = db.scalar(
            select(ProductSnapshot)
            .where(ProductSnapshot.product_id == product.id, ProductSnapshot.status == "VALID")
            .order_by(ProductSnapshot.captured_at.desc(), ProductSnapshot.id.desc())
            .limit(1)
        )
        rows.append(
            {
                "task": task,
                "product": product,
                "store": product.store,
                "last_success_at": latest_success.captured_at if latest_success else None,
                "failed_at": task.last_attempt.attempted_at if task.last_attempt else None,
            }
        )
    return templates.TemplateResponse(
        request,
        "collection_pending.html",
        {"request": request, "rows": rows, "status": _collection_status(db)},
    )


@router.get("/dashboard/stores/{store_id}", response_class=HTMLResponse)
def store_detail(
    store_id: int, request: Request, db: Session = Depends(get_db)
) -> HTMLResponse:
    store = db.get(Store, store_id)
    if store is None:
        raise HTTPException(status_code=404, detail="Store not found")
    products = list(
        db.scalars(
            select(Product)
            .where(Product.store_id == store_id, Product.status == "active")
            .order_by(Product.id.desc())
        )
    )
    metrics = list(
        db.scalars(
            select(StoreDailyMetric)
            .where(StoreDailyMetric.store_id == store_id)
            .order_by(StoreDailyMetric.metric_date.desc())
            .limit(90)
        )
    )
    yesterday = datetime.now(ZoneInfo(get_settings().timezone)).date() - timedelta(days=1)
    top_products = store_top_products(db, store_id, yesterday, 20)
    candidate_products = list(
        db.scalars(
            select(Product)
            .where(Product.store_id == store_id, Product.status == "active")
            .order_by(Product.discovery_rank.is_(None), Product.discovery_rank, Product.id)
            .limit(20)
        )
    )
    latest_discovery = db.scalar(
        select(StoreSnapshot)
        .where(StoreSnapshot.store_id == store_id)
        .order_by(StoreSnapshot.collected_at.desc(), StoreSnapshot.id.desc())
        .limit(1)
    )
    history_dates = [
        datetime.now(ZoneInfo(get_settings().timezone)).date() - timedelta(days=offset)
        for offset in range(7, 0, -1)
    ]
    product_rows = _product_monitor_rows(db, products, history_dates)
    return templates.TemplateResponse(
        request,
        "store_detail.html",
        {
            "request": request,
            "store": store,
            "products": products,
            "metrics": metrics,
            "yesterday": yesterday,
            "top_products": top_products,
            "candidate_products": candidate_products,
            "latest_discovery": latest_discovery,
            "history_dates": history_dates,
            "product_rows": product_rows,
        },
    )
