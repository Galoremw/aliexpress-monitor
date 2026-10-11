from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import BrowserStoreDiscoveryRequest
from app.api.utils import extract_product_id, extract_store_id, normalize_aliexpress_url
from app.db.models import BrowserCollectionItem, Product, ProductDailyMetric, ProductSnapshot, Store, StoreSnapshot
from app.services.historical_metrics import looks_cumulative_like_daily_values
from app.services.metrics import calculate_store_daily_metric


def save_monitored_store_observation(
    db: Session, item: BrowserCollectionItem, payload: BrowserStoreDiscoveryRequest
) -> tuple[StoreSnapshot, bool, dict]:
    store = db.get(Store, item.store_id)
    if store is None or store.status != "active":
        raise ValueError("监控店铺不存在或已停用")
    raw = payload.raw_data
    if raw.get("collection_mode") != "monitored_store_daily":
        raise ValueError("旧版店铺采集结果缺少固定监控池协议，请重载扩展")
    targets = list(db.scalars(select(Product).where(
        Product.store_id == store.id, Product.status == "active"
    ).order_by(Product.aliexpress_product_id)))
    target_ids = [product.aliexpress_product_id for product in targets]
    submitted_targets = raw.get("monitored_product_ids")
    if (not target_ids or not isinstance(submitted_targets, list)
            or sorted(submitted_targets) != target_ids):
        raise ValueError("监控商品 ID 已变化，请重新领取店铺任务")
    if raw.get("history_target_date") != item.run.target_date.isoformat():
        raise ValueError("采集日期与任务日期不一致")
    urls = raw.get("visited_page_urls")
    if not isinstance(urls, list) or not urls or len(urls) > 10:
        raise ValueError("店铺页访问记录缺失或超过上限")
    for page_url in [payload.url, *urls]:
        normalized = normalize_aliexpress_url(page_url, "store")
        parsed = urlsplit(normalized)
        if (extract_store_id(normalized) != store.aliexpress_store_id
                or parsed.path != f"/store/{store.aliexpress_store_id}/pages/all-items.html"
                or parse_qs(parsed.query).get("shop_sortType") != ["orders_desc"]):
            raise ValueError("结果包含非目标店铺或非 Orders 排序页面")
    if payload.platform_store_id != store.aliexpress_store_id:
        raise ValueError("店铺 ID 与任务不一致")

    expected = {item.run.target_date - timedelta(days=offset) for offset in range(1, 8)}
    target_by_id = {product.aliexpress_product_id: product for product in targets}
    submitted: dict[str, tuple[object, dict]] = {}
    for candidate in payload.products:
        product_id = candidate.platform_product_id
        if product_id not in target_by_id or product_id in submitted:
            raise ValueError("结果包含非监控商品或重复商品 ID")
        if extract_product_id(normalize_aliexpress_url(candidate.url, "product")) != product_id:
            raise ValueError("商品链接与 IXSPY 图表 ID 不一致")
        if candidate.historical_sales and candidate.history_source != "ixspy_visible_daily_increment":
            raise ValueError("仅接受 IXSPY 可见日增量")
        values = {}
        for point in candidate.historical_sales:
            if point.value_type != "daily_increment" or point.date not in expected or point.date in values:
                raise ValueError("日增量日期、类型或唯一性不正确")
            values[point.date] = point.value
        if len(values) == 7 and looks_cumulative_like_daily_values(
            [values[day] for day in sorted(expected)]
        ):
            raise ValueError("疑似累计销量曲线，拒绝作为日增量入库")
        submitted[product_id] = (candidate, values)

    diagnostics = {
        "target_ids": target_ids,
        "found_ids": sorted(submitted),
        "missing_ids": sorted(set(target_ids) - set(submitted)),
        "missing_dates": {},
        "visited_page_urls": urls,
    }
    snapshot = StoreSnapshot(
        store_id=store.id,
        collected_at=datetime.now(timezone.utc),
        collector_name="chrome_extension_store",
        collector_version=str(raw.get("extractor_version", "unknown")),
        parse_status="partial",
        candidate_count=len(submitted),
        raw_payload={"collection_mode": "monitored_store_daily", "diagnostics": diagnostics},
    )
    db.add(snapshot)
    db.flush()
    for product_id, product in target_by_id.items():
        candidate, values = submitted.get(product_id, (None, {}))
        product_snapshot = None
        if candidate is not None:
            product_snapshot = ProductSnapshot(
                product_id=product.id,
                collected_at=datetime.now(timezone.utc),
                captured_at=datetime.now(timezone.utc),
                collector_name="chrome_extension_store",
                collector_version=str(raw.get("extractor_version", "unknown")),
                parse_status="success" if len(values) == 7 else "partial",
                source="CHROME_EXTENSION",
                status="VALID" if len(values) == 7 else "SUSPECT",
                title=candidate.title,
                raw_data={
                    "collection_mode": "monitored_store_daily",
                    "history_source": candidate.history_source,
                    "historical_sales": [point.model_dump(mode="json") for point in candidate.historical_sales],
                    "history_target_date": item.run.target_date.isoformat(),
                },
            )
            db.add(product_snapshot)
            db.flush()
        missing = sorted(expected - values.keys())
        if missing:
            diagnostics["missing_dates"][product_id] = [day.isoformat() for day in missing]
        for metric_date in sorted(expected):
            metric = db.scalar(select(ProductDailyMetric).where(
                ProductDailyMetric.product_id == product.id,
                ProductDailyMetric.metric_date == metric_date,
            ))
            if (metric_date not in values and metric is not None and metric.is_estimable
                    and metric.calculation_method == "ixspy_visible_daily_increment"):
                continue
            if metric is None:
                metric = ProductDailyMetric(product_id=product.id, metric_date=metric_date)
                db.add(metric)
            observed = metric_date in values
            metric.estimated_sales = values[metric_date] if observed else 0
            metric.is_estimable = observed
            metric.reason = "ixspy_visible_daily_increment" if observed else "ixspy_store_page_pending"
            metric.start_snapshot_id = None
            metric.end_snapshot_id = product_snapshot.id if product_snapshot else None
            metric.calculation_method = "ixspy_visible_daily_increment" if observed else "ixspy_store_page_pending"
            metric.calculated_at = datetime.now(timezone.utc)
    complete = not diagnostics["missing_dates"]
    snapshot.parse_status = "success" if complete else "partial"
    snapshot.raw_payload = {"collection_mode": "monitored_store_daily", "diagnostics": diagnostics}
    db.flush()
    for metric_date in sorted(expected):
        calculate_store_daily_metric(db, store.id, metric_date, commit=False)
    return snapshot, complete, diagnostics
