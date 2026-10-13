from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import BrowserStoreDiscoveryRequest, BrowserStoreProduct
from app.api.utils import extract_product_id, extract_store_id, normalize_aliexpress_url
from app.db.models import (
    BrowserCollectionItem,
    Product,
    ProductDailyMetric,
    ProductSnapshot,
    Store,
    StoreSnapshot,
)
from app.services.historical_metrics import import_historical_sales, looks_cumulative_like_daily_values
from app.services.metrics import calculate_product_daily_metrics, calculate_store_daily_metric
from app.services.product_presentation import clean_product_title
from app.services.snapshots import save_store_product_snapshot
from app.services.store_page_discovery import (
    advance_store_page_cursor,
    candidate_daily_values,
    get_or_create_store_page_discovery_state,
)


def _diagnostic_records(raw: dict, key: str, limit: int) -> list[dict]:
    records = raw.get(key)
    if not isinstance(records, list):
        return []
    return [record for record in records[:limit] if isinstance(record, dict)]


def _validated_orders_url(store: Store, value: str) -> str:
    try:
        normalized = normalize_aliexpress_url(value, "store")
    except HTTPException as exc:
        raise ValueError("店铺 Orders 页面 URL 无效") from exc
    parsed = urlsplit(normalized)
    if (
        extract_store_id(normalized) != store.aliexpress_store_id
        or parsed.path != f"/store/{store.aliexpress_store_id}/pages/all-items.html"
        or parse_qs(parsed.query).get("shop_sortType") != ["orders_desc"]
    ):
        raise ValueError("结果包含非目标店铺或非 Orders 排序页面")
    return normalized


def _orders_url_identity(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return f"invalid:{value}"
    query = parse_qsl(parsed.query, keep_blank_values=True)
    page = next((item for key, item in query if key.lower() in {"page", "pagenum", "pageno"}), "1")
    remaining = [
        (key.lower(), item)
        for key, item in query
        if key.lower() not in {"page", "pagenum", "pageno", "spm"}
    ]
    if not any(key == "sorttype" for key, _ in remaining):
        remaining.append(("sorttype", "bestmatch_sort"))
    remaining.append(("page", page))
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, urlencode(sorted(remaining)), "")
    )


def _process_candidate_page(
    db: Session,
    store: Store,
    page,
    target_date,
    raw: dict,
    scan: dict,
    page_url_error: str | None,
) -> bool:
    if page is None:
        return True

    active_products = list(
        db.scalars(
            select(Product)
            .where(Product.store_id == store.id, Product.status == "active")
            .order_by(Product.id)
        )
    )
    if len(active_products) >= 20:
        scan["status"] = "pool_full"
        for candidate in page.candidates:
            scan["skipped_ids"].append(candidate.platform_product_id)
            scan["skip_reasons"].append(
                {"product_id": candidate.platform_product_id, "reason": "pool_full"}
            )
        return True

    state = get_or_create_store_page_discovery_state(db, store)
    cursor_before = state.next_page_url
    cursor = advance_store_page_cursor(
        db,
        store,
        scanned_url=page.page_url,
        has_next_page=page.has_next_page,
        next_page_url=page.next_page_url,
        scan_complete=page.scan_complete,
        pagination_known=page.pagination_known,
        error=page_url_error or "IXSPY 候选商品图表页读取未完成",
    )
    scan["cursor_before"] = cursor_before
    scan["cursor_after"] = cursor.next_page_url
    scan["error"] = cursor.last_error
    scan["status"] = cursor.last_outcome or "rejected"

    if cursor.last_outcome not in {"advanced", "wrapped", "pagination_unknown"}:
        reason = "candidate_page_incomplete" if cursor.last_outcome == "incomplete" else "cursor_not_advanced"
        for candidate in page.candidates:
            scan["skipped_ids"].append(candidate.platform_product_id)
            scan["skip_reasons"].append(
                {"product_id": candidate.platform_product_id, "reason": reason}
            )
        return cursor.last_outcome == "duplicate"

    expected_dates = {target_date - timedelta(days=offset) for offset in range(1, 8)}
    active_ids = {product.aliexpress_product_id for product in active_products}
    inactive_ids = set(
        db.scalars(
            select(Product.aliexpress_product_id).where(
                Product.store_id == store.id, Product.status == "inactive"
            )
        )
    )
    eligible: list[tuple[BrowserStoreProduct, str, dict, int, int]] = []
    seen_ids: set[str] = set()
    for orders_rank, candidate in enumerate(page.candidates, start=1):
        product_id = candidate.platform_product_id
        reason = None
        if product_id in seen_ids:
            reason = "duplicate_candidate"
        else:
            seen_ids.add(product_id)
            if product_id in active_ids:
                reason = "already_monitored"
            elif product_id in inactive_ids:
                reason = "inactive_product_not_reactivated"

        product_url = None
        if reason is None:
            try:
                product_url = normalize_aliexpress_url(candidate.url, "product")
                if extract_product_id(product_url) != product_id:
                    reason = "invalid_product_url"
            except HTTPException:
                reason = "invalid_product_url"

        values = candidate_daily_values(candidate, expected_dates) if reason is None else None
        if reason is None and values is None:
            reason = "incomplete_or_cumulative_ixspy"
        if reason is None:
            conflict = db.scalar(select(Product).where(Product.url == product_url))
            if conflict is not None and conflict.store_id != store.id:
                reason = "product_url_owned_by_other_store"

        if reason is not None:
            scan["skipped_ids"].append(product_id)
            scan["skip_reasons"].append({"product_id": product_id, "reason": reason})
        else:
            eligible.append(
                (candidate, product_url, values, orders_rank, sum(values.values()))
            )

    eligible.sort(key=lambda row: (-row[4], row[3]))
    slots = max(0, 20 - len(active_products))
    captured_at = datetime.now(timezone.utc)
    for candidate, product_url, values, orders_rank, _ in eligible:
        if slots <= 0:
            product_id = candidate.platform_product_id
            scan["skipped_ids"].append(product_id)
            scan["skip_reasons"].append({"product_id": product_id, "reason": "pool_full"})
            continue

        product = Product(
            store_id=store.id,
            aliexpress_product_id=candidate.platform_product_id,
            url=product_url,
            title=clean_product_title(candidate.title),
            discovery_source="chrome_extension_store",
            discovery_rank=orders_rank,
            discovered_at=captured_at,
        )
        db.add(product)
        db.flush()
        product_snapshot = save_store_product_snapshot(
            db,
            product,
            captured_at=captured_at,
            sold_count=None,
            title=candidate.title,
            source="CHROME_EXTENSION",
            collector_name="chrome_extension_store",
            collector_version=str(raw.get("extractor_version", "unknown"))[:32],
            raw_payload={
                "collection_mode": "store_page_pool_fill",
                "rank": orders_rank,
                "product_id": candidate.platform_product_id,
                "url": product.url,
                "title": candidate.title,
                "main_image_url": candidate.image_url,
                "extension_raw_data": raw,
            },
            historical_sales=candidate.historical_sales,
            history_target_date=target_date,
            history_source=candidate.history_source,
            commit=False,
        )
        import_historical_sales(
            db,
            product.id,
            product_snapshot.id,
            candidate.historical_sales,
            target_date=target_date,
            commit=False,
        )
        calculate_product_daily_metrics(db, product.id, commit=False)
        scan["added_ids"].append(candidate.platform_product_id)
        slots -= 1

    return True


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
    if (
        not isinstance(submitted_targets, list)
        or not all(isinstance(product_id, str) for product_id in submitted_targets)
        or sorted(submitted_targets) != target_ids
    ):
        raise ValueError("监控商品 ID 已变化，请重新领取店铺任务")
    if raw.get("history_target_date") != item.run.target_date.isoformat():
        raise ValueError("采集日期与任务日期不一致")
    urls = raw.get("visited_page_urls")
    if not isinstance(urls, list) or not urls:
        raise ValueError("店铺页访问记录缺失")
    normalized_visited = [_validated_orders_url(store, page_url) for page_url in [payload.url, *urls]]
    candidate_page = payload.candidate_page
    candidate_page_url = None
    candidate_page_url_error = None
    if candidate_page is not None:
        try:
            candidate_page_url = _validated_orders_url(store, candidate_page.page_url)
        except ValueError as exc:
            candidate_page_url_error = str(exc)
    visited_identities = {_orders_url_identity(value) for value in normalized_visited}
    if candidate_page is not None:
        visited_identities.add(
            _orders_url_identity(candidate_page_url or candidate_page.page_url)
        )
    if len(visited_identities) > 11:
        raise ValueError("店铺任务访问的唯一 Orders 页面不能超过 11 页")
    if payload.platform_store_id != store.aliexpress_store_id:
        raise ValueError("店铺 ID 与任务不一致")

    expected = {item.run.target_date - timedelta(days=offset) for offset in range(1, 8)}
    discovery_state = get_or_create_store_page_discovery_state(db, store)
    cursor_before = discovery_state.next_page_url
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
        "visited_unique_page_count": len(visited_identities),
        "worker_id": raw.get("worker_id")[:128] if isinstance(raw.get("worker_id"), str) else None,
        "worker_label": raw.get("worker_label")[:128] if isinstance(raw.get("worker_label"), str) else None,
        "page_scans": _diagnostic_records(raw, "page_scans", 10),
        "chart_reads": _diagnostic_records(raw, "chart_reads", 20),
        "candidate_scan": {
            "status": "not_requested",
            "page_url": candidate_page.page_url if candidate_page else None,
            "scan_complete": candidate_page.scan_complete if candidate_page else None,
            "pagination_known": candidate_page.pagination_known if candidate_page else None,
            "has_next_page": candidate_page.has_next_page if candidate_page else None,
            "next_page_url": candidate_page.next_page_url if candidate_page else None,
            "candidate_ids": [candidate.platform_product_id for candidate in candidate_page.candidates]
            if candidate_page else [],
            "added_ids": [],
            "skipped_ids": [],
            "skip_reasons": [],
            "cursor_before": cursor_before,
            "cursor_after": cursor_before,
            "page_diagnostics": candidate_page.diagnostics if candidate_page else {},
            "error": None,
        },
    }
    snapshot = StoreSnapshot(
        store_id=store.id,
        collected_at=datetime.now(timezone.utc),
        collector_name="chrome_extension_store",
        collector_version=str(raw.get("extractor_version", "unknown")),
        parse_status="partial",
        candidate_count=len(submitted) + (len(candidate_page.candidates) if candidate_page else 0),
        raw_payload={"collection_mode": "monitored_store_daily", "diagnostics": deepcopy(diagnostics)},
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
    candidate_scan_complete = _process_candidate_page(
        db,
        store,
        candidate_page,
        item.run.target_date,
        raw,
        diagnostics["candidate_scan"],
        candidate_page_url_error,
    )
    complete = not diagnostics["missing_dates"] and candidate_scan_complete
    snapshot.parse_status = "success" if complete else "partial"
    snapshot.raw_payload = {
        "collection_mode": "monitored_store_daily",
        "diagnostics": deepcopy(diagnostics),
    }
    db.flush()
    for metric_date in sorted(expected):
        calculate_store_daily_metric(db, store.id, metric_date, commit=False)
    return snapshot, complete, diagnostics
