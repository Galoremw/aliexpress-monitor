from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unicodedata
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
    StoreUrlIdentity,
)
from app.services.historical_metrics import import_historical_sales, looks_cumulative_like_daily_values
from app.services.metrics import calculate_product_daily_metrics, calculate_store_daily_metric
from app.services.product_presentation import clean_product_title
from app.services.snapshots import save_store_product_snapshot
from app.services.store_identity import normalize_store_orders_identity
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


def _identity_title_key(value: str | None) -> str | None:
    if not value:
        return None
    title = clean_product_title(value) or ""
    if title.startswith("已采集"):
        title = title[len("已采集"):].strip()
    key = "".join(
        character.casefold()
        for character in unicodedata.normalize("NFKC", title)
        if character.isalnum()
    )
    return key if len(key) >= 12 else None


def _normalized_store_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _validate_observed_product_identity(
    store: Store,
    target: Product,
    candidate: BrowserStoreProduct,
    visited_identities: set[str],
    raw: dict,
    route_alias_ids: set[str],
) -> dict | None:
    observed_id = candidate.observed_product_id or candidate.platform_product_id
    if observed_id == target.aliexpress_product_id:
        return None
    if candidate.identity_match_method != "exact_title":
        raise ValueError("监控商品 ID 不一致，缺少唯一标题匹配证据")
    if not candidate.title or _identity_title_key(candidate.title) != _identity_title_key(target.title):
        raise ValueError("监控商品 ID 不一致，页面商品标题不匹配")
    try:
        observed_url = normalize_aliexpress_url(candidate.observed_url or "", "product")
        observed_page_url = _validated_orders_url(
            store, candidate.observed_page_url or "", route_alias_ids=route_alias_ids
        )
    except HTTPException as exc:
        raise ValueError("监控商品 ID 不一致，页面商品地址无效") from exc
    if extract_product_id(observed_url) != observed_id:
        raise ValueError("页面商品链接与观察到的商品 ID 不一致")
    if _orders_url_identity(observed_page_url, store.aliexpress_store_id) not in visited_identities:
        raise ValueError("商品 ID 匹配来源不在本次已访问的店铺页中")
    page_scan = next((
        record for record in _diagnostic_records(raw, "page_scans", 10)
        if record.get("url") == candidate.observed_page_url
    ), None)
    if page_scan is None:
        raise ValueError("商品 ID 匹配缺少店铺页扫描记录")
    visible_ids = {str(value) for value in page_scan.get("visible_product_ids", [])}
    identity_matches = page_scan.get("identity_matches", [])
    identity_matches = identity_matches if isinstance(identity_matches, list) else []
    if observed_id not in visible_ids or not any(
        isinstance(match, dict)
        and match.get("monitored_product_id") == target.aliexpress_product_id
        and match.get("observed_product_id") == observed_id
        and match.get("match_method") == "exact_title"
        for match in identity_matches
    ):
        raise ValueError("商品 ID 匹配与店铺页扫描证据不一致")
    return {
        "monitored_product_id": target.aliexpress_product_id,
        "observed_product_id": observed_id,
        "observed_url": observed_url,
        "observed_page_url": observed_page_url,
        "match_method": "exact_title",
    }


def _validated_orders_url(
    store: Store, value: str, *, route_alias_ids: set[str] | None = None
) -> str:
    try:
        normalized = normalize_aliexpress_url(value, "store")
    except HTTPException as exc:
        raise ValueError("店铺 Orders 页面 URL 无效") from exc
    parsed = urlsplit(normalized)
    route_store_id = extract_store_id(normalized)
    allowed_ids = {store.aliexpress_store_id, *(route_alias_ids or set())}
    if route_store_id not in allowed_ids:
        raise ValueError(
            f"店铺 Orders 路由 ID {route_store_id or '未识别'} 未通过本次页面身份核验"
        )
    if parsed.path != f"/store/{route_store_id}/pages/all-items.html":
        raise ValueError("访问页面不是店铺 all-items 商品列表页")
    if parse_qs(parsed.query).get("shop_sortType") != ["orders_desc"]:
        raise ValueError("访问的店铺商品页不是 Orders 降序排序")
    return normalized


def _orders_url_identity(value: str, canonical_store_id: str | None = None) -> str:
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
    path = parsed.path
    if canonical_store_id and extract_store_id(value):
        path = f"/store/{canonical_store_id}/pages/all-items.html"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, urlencode(sorted(remaining)), ""))


def _verified_route_alias_ids(
    db: Session,
    store: Store,
    raw: dict,
    candidate_page: BrowserStoreCandidatePage | None,
) -> set[str]:
    expected_store_id = store.aliexpress_store_id
    approved_aliases = {
        alias.route_store_id: alias
        for alias in db.scalars(
            select(StoreUrlIdentity).where(StoreUrlIdentity.store_id == store.id)
        )
    }
    evidence: list[tuple[dict, str | None]] = []
    identity = raw.get("store_identity")
    if isinstance(identity, dict):
        evidence.append((identity, None))
    for page_scan in _diagnostic_records(raw, "page_scans", 10):
        scan_identity = page_scan.get("store_identity")
        scan_url = page_scan.get("url")
        if isinstance(scan_identity, dict):
            evidence.append((scan_identity, scan_url if isinstance(scan_url, str) else None))
    if candidate_page is not None:
        page_scan = candidate_page.diagnostics.get("page_scan")
        if isinstance(page_scan, dict) and isinstance(page_scan.get("store_identity"), dict):
            scan_url = page_scan.get("url")
            evidence.append((page_scan["store_identity"], scan_url if isinstance(scan_url, str) else None))

    aliases = set()
    conflicting_aliases = set()
    for page_identity, page_url in evidence:
        route_store_id = page_identity.get("route_store_id")
        heading_store_id = page_identity.get("heading_store_id")
        labeled_store_ids = page_identity.get("labeled_store_ids", [])
        if not isinstance(route_store_id, str) or not route_store_id.isdigit():
            continue
        approved_alias = approved_aliases.get(route_store_id)
        if approved_alias is not None and route_store_id != expected_store_id:
            has_id_conflict = (
                heading_store_id is not None and heading_store_id != expected_store_id
                or not isinstance(labeled_store_ids, list)
                or any(value != expected_store_id for value in labeled_store_ids)
            )
            saved_name = approved_alias.evidence.get("ixspy_store_name")
            current_name = page_identity.get("ixspy_store_name")
            names_conflict = bool(
                isinstance(saved_name, str)
                and saved_name.strip()
                and isinstance(current_name, str)
                and current_name.strip()
                and _normalized_store_name(saved_name) != _normalized_store_name(current_name)
            )
            if has_id_conflict or names_conflict:
                conflicting_aliases.add(route_store_id)
            elif page_identity.get("identity_method") == "backend_approved_url_alias":
                observed_page_url = page_identity.get("page_url") or page_url
                try:
                    observed_route_id, observed_identity = normalize_store_orders_identity(
                        observed_page_url
                    )
                except (TypeError, ValueError):
                    pass
                else:
                    if (
                        page_identity.get("target_store_id") == expected_store_id
                        and observed_route_id == route_store_id
                        and approved_alias.route_store_id == observed_route_id
                        and approved_alias.normalized_page_identity == observed_identity
                        and (not page_url or page_url == observed_page_url)
                    ):
                        aliases.add(route_store_id)
        has_matching_identity = (
            heading_store_id == expected_store_id
            or isinstance(labeled_store_ids, list)
            and expected_store_id in labeled_store_ids
        )
        has_conflicting_identity = (
            heading_store_id is not None and heading_store_id != expected_store_id
            or not isinstance(labeled_store_ids, list)
            or any(value != expected_store_id for value in labeled_store_ids)
        )
        if (
            page_identity.get("target_store_id") != expected_store_id
            or not has_matching_identity
            or has_conflicting_identity
        ):
            continue
        if page_url and extract_store_id(page_url) != route_store_id:
            continue
        if route_store_id != expected_store_id:
            aliases.add(route_store_id)
    return aliases - conflicting_aliases


def _process_candidate_page(
    db: Session,
    store: Store,
    page,
    target_date,
    raw: dict,
    scan: dict,
    page_url_error: str | None,
    route_alias_id: str | None = None,
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
        route_alias_id=route_alias_id,
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
    inactive_products = {
        product.aliexpress_product_id: product
        for product in db.scalars(
            select(Product).where(
                Product.store_id == store.id, Product.status == "inactive"
            )
        )
    }
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
            elif (
                product_id in inactive_products
                and not inactive_products[product_id].pool_reset_reactivation_eligible
            ):
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

        product = inactive_products.get(candidate.platform_product_id)
        if product is None:
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
        else:
            product.status = "active"
            product.pool_reset_reactivation_eligible = False
            product.url = product_url
            product.title = clean_product_title(candidate.title)
            product.discovery_source = "chrome_extension_store"
            product.discovery_rank = orders_rank
            product.discovered_at = product.discovered_at or captured_at
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
    candidate_page = payload.candidate_page
    route_alias_ids = _verified_route_alias_ids(db, store, raw, candidate_page)
    identity = raw.get("store_identity")
    if (
        isinstance(identity, dict)
        and identity.get("route_store_id") != store.aliexpress_store_id
        and identity.get("route_store_id") not in route_alias_ids
    ):
        raise ValueError("店铺页面路径别名缺少匹配任务目标的页面身份依据")
    urls = raw.get("visited_page_urls")
    if not isinstance(urls, list) or not urls:
        raise ValueError("店铺页访问记录缺失")
    normalized_visited = [
        _validated_orders_url(store, page_url, route_alias_ids=route_alias_ids)
        for page_url in [payload.url, *urls]
    ]
    candidate_page_url = None
    candidate_page_url_error = None
    if candidate_page is not None:
        try:
            candidate_page_url = _validated_orders_url(
                store, candidate_page.page_url, route_alias_ids=route_alias_ids
            )
        except ValueError as exc:
            candidate_page_url_error = str(exc)
    visited_identities = {
        _orders_url_identity(value, store.aliexpress_store_id) for value in normalized_visited
    }
    if candidate_page is not None:
        visited_identities.add(
            _orders_url_identity(candidate_page_url or candidate_page.page_url, store.aliexpress_store_id)
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
    identity_matches = []
    for candidate in payload.products:
        product_id = candidate.platform_product_id
        if product_id not in target_by_id or product_id in submitted:
            raise ValueError("结果包含非监控商品或重复商品 ID")
        if extract_product_id(normalize_aliexpress_url(candidate.url, "product")) != product_id:
            raise ValueError("商品链接与 IXSPY 图表 ID 不一致")
        identity_match = _validate_observed_product_identity(
            store, target_by_id[product_id], candidate, visited_identities, raw, route_alias_ids,
        )
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
        submitted[product_id] = (candidate, values, identity_match)
        if identity_match:
            identity_matches.append(identity_match)

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
        "identity_matches": identity_matches,
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
        candidate, values, identity_match = submitted.get(product_id, (None, {}, None))
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
                    **({"identity_match": identity_match} if identity_match else {}),
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
        extract_store_id(candidate_page_url)
        if candidate_page_url and extract_store_id(candidate_page_url) in route_alias_ids
        else None,
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
