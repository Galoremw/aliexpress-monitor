from datetime import date, datetime, timedelta
from types import SimpleNamespace
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
    ProductReview,
    ReviewCollectionTask,
    Store,
    StoreDailyMetric,
    StoreSnapshot,
)
from app.core.auth import require_page_auth
from app.db.session import get_db
from app.core.config import get_settings
from app.services.store_discovery import store_top_products
from app.services.collection_progress import (
    build_collection_progress,
    manual_completed_product_ids,
    snapshot_collection_state,
)
from app.services.browser_collection import (
    browser_collection_control_status,
    get_today_run,
    serialize_run,
)
from app.services.reviews import get_insights, serialize_task, task_query
from app.services.product_presentation import clean_product_title, product_image_url

router = APIRouter(include_in_schema=False, dependencies=[Depends(require_page_auth)])
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["clean_product_title"] = clean_product_title


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
    metric_start = history_dates[0]
    metrics = list(
        db.scalars(
            select(ProductDailyMetric).where(
                ProductDailyMetric.product_id.in_(product_ids),
                ProductDailyMetric.metric_date >= metric_start,
                ProductDailyMetric.metric_date <= history_dates[-1],
                ProductDailyMetric.calculation_method == "ixspy_visible_daily_increment",
            )
        )
    )
    metric_by_key = {}
    display_dates = set(history_dates)
    for metric in metrics:
        display_date = metric.metric_date
        if display_date not in display_dates:
            continue
        key = (metric.product_id, display_date)
        metric_by_key[key] = SimpleNamespace(
            metric_date=display_date,
            estimated_sales=metric.estimated_sales,
            is_estimable=metric.is_estimable,
            reason=metric.reason,
            calculation_method=metric.calculation_method,
        )
    latest_snapshot_by_product: dict[int, ProductSnapshot] = {}
    latest_history_snapshot_by_product: dict[int, ProductSnapshot] = {}
    image_by_product: dict[int, str] = {}
    for snapshot in db.scalars(
        select(ProductSnapshot)
        .where(ProductSnapshot.product_id.in_(product_ids))
        .order_by(ProductSnapshot.collected_at.desc(), ProductSnapshot.id.desc())
    ):
        latest_snapshot_by_product.setdefault(snapshot.product_id, snapshot)
        raw_data = snapshot.raw_data if isinstance(snapshot.raw_data, dict) else {}
        image_url = product_image_url(raw_data) or product_image_url(snapshot.raw_payload)
        if image_url:
            image_by_product.setdefault(snapshot.product_id, image_url)
        history_points = raw_data.get("historical_sales")
        history_points = [
            point for point in history_points
            if isinstance(point, dict) and point.get("value_type") == "daily_increment"
        ] if isinstance(history_points, list) else []
        if history_points and snapshot.product_id not in latest_history_snapshot_by_product:
            latest_history_snapshot_by_product[snapshot.product_id] = snapshot

    rows = []
    latest_date = history_dates[-1]
    for product in products:
        history = []
        for metric_date in history_dates:
            metric = metric_by_key.get((product.id, metric_date))
            has_value = bool(metric and metric.is_estimable and metric.estimated_sales is not None)
            source_method = metric.calculation_method if has_value else None
            history.append(
                {
                    "date": metric_date,
                    "value": metric.estimated_sales if has_value else None,
                    "reason": metric.reason if metric else "no_snapshot",
                    "source_method": source_method,
                    "source_label": (
                        "IXSPY 页面可见日增量"
                        if source_method == "ixspy_visible_daily_increment"
                        else "等待 IXSPY 日增量"
                    ),
                }
            )
        estimable_values = [item["value"] for item in history if item["value"] is not None]
        latest_metric = metric_by_key.get((product.id, latest_date))
        latest_snapshot = latest_snapshot_by_product.get(product.id)
        history_snapshot = latest_history_snapshot_by_product.get(product.id)
        history_snapshot_data = history_snapshot.raw_data if history_snapshot else {}
        history_points = history_snapshot_data.get("historical_sales", [])
        history_points = [
            point for point in history_points
            if isinstance(point, dict) and point.get("value_type") == "daily_increment"
        ] if isinstance(history_points, list) else []
        latest_snapshot_data = latest_snapshot.raw_data if latest_snapshot and isinstance(latest_snapshot.raw_data, dict) else {}
        history_source = latest_snapshot_data.get("history_source")
        status_class, status_label = _snapshot_status(latest_snapshot)
        collection_state, collection_label, collection_status_class, collection_captured_at = snapshot_collection_state(
            latest_snapshot,
            latest_date + timedelta(days=1),
            ZoneInfo(get_settings().timezone),
        )
        rows.append(
            {
                "product": product,
                "display_title": clean_product_title(product.title)
                or product.aliexpress_product_id,
                "main_image_url": image_by_product.get(product.id),
                "latest_metric": latest_metric,
                "history": history,
                "history_coverage": sum(
                    item["source_method"] == "ixspy_visible_daily_increment"
                    for item in history
                ),
                "seven_day_total": sum(estimable_values)
                if len(estimable_values) == 7
                and sum(
                    item["source_method"] == "ixspy_visible_daily_increment"
                    for item in history
                )
                == 7
                else None,
                "coverage": len(estimable_values),
                "latest_snapshot": latest_snapshot,
                "history_snapshot": history_snapshot,
                "history_points_count": len(history_points),
                "history_source": history_source
                if history_source == "ixspy_visible_daily_increment"
                else None,
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
    product_by_id = {product.id: product for product in all_products}
    for snapshot in recent_snapshots:
        status_class, status_label = _snapshot_status(snapshot)
        product = product_by_id.get(snapshot.product_id)
        recent_snapshot_rows.append(
            {
                "snapshot": snapshot,
                "display_title": (
                    clean_product_title(product.title) or product.aliexpress_product_id
                    if product
                    else str(snapshot.product_id)
                ),
                "status_class": status_class,
                "status_label": status_label,
            }
        )
    growth_metrics_raw = list(
        db.execute(
            select(ProductDailyMetric, Product)
            .join(Product, Product.id == ProductDailyMetric.product_id)
            .where(
                ProductDailyMetric.is_estimable.is_(True),
                ProductDailyMetric.calculation_method == "ixspy_visible_daily_increment",
                Product.status == "active",
            )
            .order_by(
                ProductDailyMetric.metric_date.desc(),
                ProductDailyMetric.estimated_sales.desc(),
            )
        ).all()
    )
    today = datetime.now(ZoneInfo(get_settings().timezone)).date()
    history_dates = [today - timedelta(days=offset) for offset in range(7, 0, -1)]
    monitor_rows = _product_monitor_rows(db, products, history_dates)
    monitor_row_by_product_id = {row["product"].id: row for row in monitor_rows}
    store_by_id = {store.id: store for store in stores}
    growth_metrics = []
    seen_growth_products = set()
    for metric, product in growth_metrics_raw:
        if product.id in seen_growth_products:
            continue
        seen_growth_products.add(product.id)
        growth_metrics.append(
            SimpleNamespace(
                metric_date=metric.metric_date,
                estimated_sales=metric.estimated_sales,
                product=product,
                display_title=clean_product_title(product.title) or product.aliexpress_product_id,
                store_name=(store_by_id.get(product.store_id).name if store_by_id.get(product.store_id) else "自定义监控"),
                main_image_url=monitor_row_by_product_id.get(product.id, {}).get("main_image_url"),
            )
        )
        if len(growth_metrics) == 20:
            break
    rows_by_store: dict[int, list[dict]] = {store.id: [] for store in stores}
    for row in monitor_rows:
        rows_by_store[row["product"].store_id].append(row)
    yesterday = history_dates[-1]
    latest_metric_by_store = {}
    latest_store_metrics = []
    for store in stores:
        if store.status != "active":
            continue
        store_rows = rows_by_store[store.id]
        estimated = [
            row["latest_metric"]
            for row in store_rows
            if row["latest_metric"] and row["latest_metric"].is_estimable
        ]
        summary = SimpleNamespace(
            store_id=store.id,
            metric_date=yesterday,
            estimated_sales=sum(metric.estimated_sales or 0 for metric in estimated),
            estimable_products=len(estimated),
            product_scope_count=len(store_rows),
            unavailable_products=len(store_rows) - len(estimated),
        )
        latest_metric_by_store[store.id] = summary
        if estimated:
            latest_store_metrics.append(summary)
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
        "store_by_id": store_by_id,
        "product_by_id": product_by_id,
        "history_dates": history_dates,
        "store_panels": store_panels,
        "collection_status": _collection_status(db),
        "collection_progress": build_collection_progress(db),
        "browser_collection_run": serialize_run(browser_collection_run)
        if browser_collection_run
        else None,
        "browser_collection_control": browser_collection_control_status(db),
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
            .where(
                ProductDailyMetric.product_id == product_id,
                ProductDailyMetric.calculation_method == "ixspy_visible_daily_increment",
            )
            .order_by(ProductDailyMetric.metric_date.desc())
            .limit(365)
        )
    )
    review_count = db.scalar(
        select(func.count()).select_from(ProductReview).where(ProductReview.product_id == product_id)
    ) or 0
    latest_reviews = list(
        db.scalars(
            select(ProductReview)
            .where(ProductReview.product_id == product_id)
            .order_by(ProductReview.review_date.desc().nullslast(), ProductReview.id.desc())
            .limit(12)
        )
    )
    latest_review_task = db.scalar(
        task_query()
        .where(ReviewCollectionTask.product_id == product_id)
        .order_by(ReviewCollectionTask.created_at.desc())
        .limit(1)
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
            "review_count": review_count,
            "reviews": latest_reviews,
            "review_task": serialize_task(latest_review_task) if latest_review_task else None,
            "max_estimate": max(
                (metric.estimated_sales or 0 for metric in metrics if metric.is_estimable),
                default=0,
            ),
        },
    )


@router.get("/reviews/insights", response_class=HTMLResponse)
def reviews_dashboard(
    request: Request,
    business_role: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if business_role not in (None, "SELF", "COMPETITOR", "UNCLASSIFIED"):
        raise HTTPException(status_code=422, detail="无效的商品归属筛选")
    insights = get_insights(db, business_role)
    review_statement = (
        select(ProductReview)
        .join(Product, Product.id == ProductReview.product_id)
        .where(Product.status == "active")
        .order_by(ProductReview.review_date.desc().nullslast(), ProductReview.id.desc())
        .limit(100)
    )
    if business_role:
        review_statement = review_statement.where(Product.business_role == business_role)
    reviews = list(db.scalars(review_statement))
    return templates.TemplateResponse(
        request,
        "reviews_insights.html",
        {
            "request": request,
            "insights": insights,
            "reviews": reviews,
            "business_role": business_role,
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
            .where(
                StoreDailyMetric.store_id == store_id,
                StoreDailyMetric.calculation_method == "sum_of_ixspy_visible_daily_increments",
                StoreDailyMetric.estimable_products > 0,
            )
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
