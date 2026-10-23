from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import BrowserStoreProduct
from app.api.utils import extract_product_id, normalize_aliexpress_url
from app.db.models import Product, Store, StorePageDiscoveryState, StoreSnapshot
from app.services.historical_metrics import looks_cumulative_like_daily_values, import_historical_sales
from app.services.metrics import calculate_product_daily_metrics, calculate_store_daily_metric
from app.services.product_presentation import clean_product_title
from app.services.snapshots import save_store_product_snapshot


_STORE_PATH = re.compile(r"^/store/(\d+)/pages/all-items\.html$")
_STORE_ROOT_PATH = re.compile(r"^/store/(\d+)/?$")
_FIRST_PAGE_KEYS = {"page", "pagenum", "pageno"}


def _platform_store_id(store: Store) -> str:
    if store.aliexpress_store_id and store.aliexpress_store_id.isdigit():
        return store.aliexpress_store_id
    match = re.search(r"/store/(\d+)(?:/|$)", urlsplit(store.url).path)
    if match:
        return match.group(1)
    raise ValueError("监控店铺缺少有效的 AliExpress 店铺 ID")


def _canonical_first_orders_url(store: Store) -> str:
    store_id = _platform_store_id(store)
    source_query = parse_qsl(urlsplit(store.url).query, keep_blank_values=True)
    query = [
        (key, value)
        for key, value in source_query
        if key.lower() not in _FIRST_PAGE_KEYS | {"sorttype", "shop_sorttype"}
    ]
    query.extend(
        [("sortType", "bestmatch_sort"), ("shop_sortType", "orders_desc")]
    )
    return urlunsplit(
        (
            "https",
            "www.aliexpress.com",
            f"/store/{store_id}/pages/all-items.html",
            urlencode(query),
            "",
        )
    )


def _validate_orders_url(
    store: Store, value: str, *, route_alias_id: str | None = None
) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("店铺 Orders 页面 URL 无效")
    try:
        parsed = urlsplit(value.strip())
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError as exc:
        raise ValueError("店铺 Orders 页面 URL 无效") from exc
    labels = hostname.split(".")
    if (
        parsed.scheme.lower() != "https"
        or len(labels) < 2
        or labels[-2] != "aliexpress"
        or not labels[-1].isalpha()
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise ValueError("店铺 Orders 页面必须使用 HTTPS AliExpress 地址")
    match = _STORE_PATH.fullmatch(parsed.path)
    root_match = _STORE_ROOT_PATH.fullmatch(parsed.path)
    store_id = _platform_store_id(store)
    route_match = match or root_match
    if route_match is None:
        raise ValueError("店铺 Orders 页面路径无效")
    allowed_route_ids = {store_id}
    if route_alias_id and route_alias_id.isdigit():
        allowed_route_ids.add(route_alias_id)
    route_store_id = route_match.group(1)
    if route_store_id not in allowed_route_ids:
        raise ValueError("店铺 Orders 页面 ID 与监控店铺不一致")
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if root_match:
        sort_values = [
            value for key, value in query
            if key.lower() in {"sorttype", "shop_sorttype"}
        ]
        if not sort_values or any(value != "orders_desc" for value in sort_values):
            raise ValueError("店铺页面必须按 Orders 降序排列")
    elif [value for key, value in query if key == "shop_sortType"] != ["orders_desc"]:
        raise ValueError("店铺页面必须按 Orders 降序排列")
    page_values = [
        item
        for key, item in query
        if key.lower() in _FIRST_PAGE_KEYS
    ]
    if len(page_values) > 1 or any(not item.isdigit() or int(item) < 1 for item in page_values):
        raise ValueError("店铺分页参数无效")
    path = parsed.path
    if root_match:
        path = f"/store/{store_id}/pages/all-items.html"
        query = [
            (key, value)
            for key, value in query
            if key.lower() not in {"sorttype", "shop_sorttype"}
        ]
        query.extend(
            [("sortType", "bestmatch_sort"), ("shop_sortType", "orders_desc")]
        )
    return urlunsplit(("https", parsed.netloc.lower(), path, urlencode(query), ""))


def _orders_page_identity(
    value: str, canonical_store_id: str | None = None
) -> tuple[str, tuple[tuple[str, str], ...]]:
    parsed = urlsplit(value)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    page = next(
        (item for key, item in query if key.lower() in _FIRST_PAGE_KEYS),
        "1",
    )
    remaining = [
        (key.lower(), item)
        for key, item in query
        if key.lower() not in _FIRST_PAGE_KEYS | {"spm"}
    ]
    try:
        normalized_page = str(int(page))
    except (TypeError, ValueError):
        normalized_page = page
    remaining.append(("page", normalized_page))
    if not any(key == "sorttype" for key, _ in remaining):
        remaining.append(("sorttype", "bestmatch_sort"))
    path = parsed.path
    if canonical_store_id and _STORE_PATH.fullmatch(path):
        path = f"/store/{canonical_store_id}/pages/all-items.html"
    return path, tuple(sorted(remaining))


def _same_orders_page(left: str, right: str, canonical_store_id: str | None = None) -> bool:
    return _orders_page_identity(left, canonical_store_id) == _orders_page_identity(
        right, canonical_store_id
    )


def _orders_page_number(value: str) -> int:
    return int(next(
        (item for key, item in parse_qsl(urlsplit(value).query, keep_blank_values=True)
         if key.lower() in _FIRST_PAGE_KEYS),
        "1",
    ))


def get_or_create_store_page_discovery_state(
    db: Session, store: Store, *, first_orders_url: str | None = None
) -> StorePageDiscoveryState:
    statement = (
        select(StorePageDiscoveryState)
        .where(StorePageDiscoveryState.store_id == store.id)
        .with_for_update()
    )
    state = db.scalar(statement)
    if state is not None:
        return state

    first_url = _validate_orders_url(
        store, first_orders_url or _canonical_first_orders_url(store)
    )
    state = StorePageDiscoveryState(
        store_id=store.id,
        first_orders_url=first_url,
        next_page_url=first_url,
        last_outcome="initialized",
    )
    db.add(state)
    db.flush()
    return state


def advance_store_page_cursor(
    db: Session,
    store: Store,
    *,
    scanned_url: str,
    has_next_page: bool,
    next_page_url: str | None,
    scan_complete: bool,
    pagination_known: bool = True,
    error: str | None = None,
    route_alias_id: str | None = None,
) -> StorePageDiscoveryState:
    state = get_or_create_store_page_discovery_state(db, store)
    try:
        scanned = _validate_orders_url(store, scanned_url, route_alias_id=route_alias_id)
    except ValueError as exc:
        state.last_outcome = "rejected"
        state.last_error = str(exc)
        db.flush()
        return state

    store_id = _platform_store_id(store)
    if not _same_orders_page(scanned, state.next_page_url, store_id):
        if state.last_successful_page_url and _same_orders_page(
            scanned, state.last_successful_page_url, store_id
        ):
            state.last_outcome = "duplicate"
            state.last_error = None
            db.flush()
            return state
        state.last_outcome = "stale"
        state.last_error = "扫描页面与当前持久化游标不一致"
        db.flush()
        return state

    if not scan_complete:
        state.last_outcome = "incomplete"
        state.last_error = (error or "店铺页面扫描未完成")[:2000]
        db.flush()
        return state

    if not pagination_known:
        state.last_outcome = "pagination_unknown"
        state.last_error = "店铺分页控件未能确认是否存在下一页"
        state.last_scanned_at = datetime.now(timezone.utc)
        db.flush()
        return state

    if has_next_page:
        if not next_page_url:
            state.last_outcome = "rejected"
            state.last_error = "存在下一页但缺少下一页 URL"
            db.flush()
            return state
        try:
            next_url = _validate_orders_url(store, next_page_url, route_alias_id=route_alias_id)
        except ValueError as exc:
            state.last_outcome = "rejected"
            state.last_error = str(exc)
            db.flush()
            return state
        if _same_orders_page(next_url, scanned, store_id):
            state.last_outcome = "rejected"
            state.last_error = "下一页 URL 与当前页相同"
            db.flush()
            return state
        if _orders_page_number(next_url) <= _orders_page_number(scanned):
            state.last_outcome = "rejected"
            state.last_error = "下一页 URL 页码未向前推进"
            db.flush()
            return state
        state.next_page_url = next_url
        state.last_outcome = "advanced"
    else:
        if next_page_url:
            state.last_outcome = "rejected"
            state.last_error = "末页结果不应包含下一页 URL"
            db.flush()
            return state
        state.next_page_url = state.first_orders_url
        state.last_outcome = "wrapped"

    state.last_successful_page_url = scanned
    state.last_scanned_at = datetime.now(timezone.utc)
    state.last_error = None
    db.flush()
    return state


def candidate_daily_values(
    candidate: BrowserStoreProduct, expected_dates: set[date]
) -> dict[date, int] | None:
    points = candidate.historical_sales
    dates = [point.date for point in points]
    if (
        not candidate.history_daily_capture_complete
        or candidate.history_source != "ixspy_visible_daily_increment"
        or candidate.history_missing_dates
        or len(candidate.history_expected_dates) != 7
        or len(set(candidate.history_expected_dates)) != 7
        or set(candidate.history_expected_dates) != expected_dates
        or len(points) != 7
        or len(set(dates)) != 7
        or set(dates) != expected_dates
        or any(point.value_type != "daily_increment" for point in points)
    ):
        return None
    values = {point.date: point.value for point in points}
    if looks_cumulative_like_daily_values([values[day] for day in sorted(expected_dates)]):
        return None
    return values


def save_store_page_candidates(
    db: Session,
    store: Store,
    *,
    page_url: str,
    candidates: Sequence[BrowserStoreProduct],
    has_next_page: bool,
    next_page_url: str | None,
    scan_complete: bool,
    source: str,
    raw_data: dict,
) -> dict:
    if store.status != "active":
        raise ValueError("监控店铺不存在或已停用")
    if raw_data.get("collection_mode") != "top_7d_increment_selection":
        raise ValueError("店铺页商品入池协议无效")
    if raw_data.get("order_sort_verified") is not True:
        raise ValueError("店铺页面未确认按 Orders 排序")
    if not scan_complete or raw_data.get("scan_complete") is not True:
        raise ValueError("店铺页面扫描未完整结束")
    if not isinstance(has_next_page, bool) or raw_data.get("has_next_page") is not has_next_page:
        raise ValueError("店铺分页诊断无效")
    pagination_known = raw_data.get("pagination_known") is True

    normalized_page = _validate_orders_url(store, page_url)
    normalized_next = None
    if has_next_page:
        if not next_page_url:
            raise ValueError("存在下一页但缺少下一页 URL")
        normalized_next = _validate_orders_url(store, next_page_url)
        if _same_orders_page(normalized_page, normalized_next):
            raise ValueError("下一页 URL 与当前页相同")
    elif next_page_url:
        raise ValueError("末页结果不应包含下一页 URL")

    state = get_or_create_store_page_discovery_state(db, store)
    cursor_before = state.next_page_url
    is_cursor_page = _same_orders_page(normalized_page, state.next_page_url)
    is_retry_page = bool(
        state.last_successful_page_url
        and _same_orders_page(normalized_page, state.last_successful_page_url)
    )
    if not is_cursor_page and not is_retry_page:
        raise ValueError("扫描页面与当前持久化游标不一致")

    target_raw = raw_data.get("history_target_date")
    try:
        target_date = date.fromisoformat(str(target_raw))
    except (TypeError, ValueError) as exc:
        raise ValueError("七日增量目标日期无效") from exc
    expected_dates = {target_date - timedelta(days=offset) for offset in range(1, 8)}
    selection_ranks = {
        str(row.get("product_id")): row.get("orders_rank")
        for row in raw_data.get("selection", [])
        if isinstance(row, dict) and row.get("product_id")
    }

    page_candidates: list[tuple[BrowserStoreProduct, str, dict[date, int] | None, int, int]] = []
    seen_ids: set[str] = set()
    diagnostics: list[dict[str, str]] = []
    for index, candidate in enumerate(candidates, start=1):
        product_id = candidate.platform_product_id
        if product_id in seen_ids:
            diagnostics.append({"product_id": product_id, "reason": "duplicate_candidate"})
            continue
        seen_ids.add(product_id)
        try:
            product_url = normalize_aliexpress_url(candidate.url, "product")
            if extract_product_id(product_url) != product_id:
                raise ValueError("商品 ID 与链接不一致")
        except (HTTPException, ValueError) as exc:
            diagnostics.append({"product_id": product_id, "reason": "invalid_product_url"})
            continue
        values = candidate_daily_values(candidate, expected_dates)
        if values is None:
            diagnostics.append({"product_id": product_id, "reason": "incomplete_or_cumulative_ixspy"})
            continue
        raw_rank = selection_ranks.get(product_id, index)
        try:
            orders_rank = int(raw_rank)
        except (TypeError, ValueError):
            orders_rank = index
        if orders_rank < 1:
            orders_rank = index
        page_candidates.append(
            (
                candidate,
                product_url,
                values,
                orders_rank,
                sum(values.values()),
            )
        )

    for _, product_url, _, _, _ in page_candidates:
        conflicting = db.scalar(select(Product).where(Product.url == product_url))
        if conflicting is not None and conflicting.store_id != store.id:
            raise ValueError("店铺页候选商品链接已归属其他监控店铺")

    cursor_result = advance_store_page_cursor(
        db,
        store,
        scanned_url=normalized_page,
        has_next_page=has_next_page,
        next_page_url=normalized_next,
        scan_complete=True,
        pagination_known=pagination_known,
    )
    if cursor_result.last_outcome not in {"advanced", "wrapped", "duplicate", "pagination_unknown"}:
        raise ValueError(cursor_result.last_error or "店铺页游标未能安全保存")

    active_products = list(
        db.scalars(
            select(Product)
            .where(
                Product.store_id == store.id,
                Product.status == "active",
                Product.monitor_scope == "competitor",
            )
            .order_by(Product.id)
        )
    )
    active_by_external_id = {item.aliexpress_product_id: item for item in active_products}
    inactive_ids = set(
        db.scalars(
            select(Product.aliexpress_product_id).where(
                Product.store_id == store.id,
                Product.status == "inactive",
                Product.monitor_scope == "competitor",
            )
        )
    )
    slots = max(0, 20 - len(active_products))
    ranked_candidates = sorted(page_candidates, key=lambda row: (-row[4], row[3]))

    captured_at = datetime.now(timezone.utc)
    added_count = 0
    existing_count = 0
    skipped_count = len(diagnostics)
    links: list[str] = []
    observed: list[tuple[Product, BrowserStoreProduct, dict[date, int], int]] = []
    for candidate, product_url, values, orders_rank, _ in ranked_candidates:
        product_id = candidate.platform_product_id
        product = active_by_external_id.get(product_id)
        if product is not None:
            existing_count += 1
        elif product_id in inactive_ids:
            diagnostics.append({"product_id": product_id, "reason": "inactive_product_not_reactivated"})
            skipped_count += 1
            continue
        elif slots <= 0:
            diagnostics.append({"product_id": product_id, "reason": "pool_full"})
            skipped_count += 1
            continue
        else:
            product = Product(
                store_id=store.id,
                aliexpress_product_id=product_id,
                url=product_url,
                title=clean_product_title(candidate.title),
                discovery_source="chrome_extension_store",
                discovery_rank=orders_rank,
                discovered_at=captured_at,
            )
            db.add(product)
            db.flush()
            active_by_external_id[product_id] = product
            slots -= 1
            added_count += 1
        if product.title is None and candidate.title:
            product.title = clean_product_title(candidate.title)
        links.append(product_url)
        observed.append((product, candidate, values, orders_rank))

    snapshot = StoreSnapshot(
        store_id=store.id,
        collected_at=captured_at,
        collector_name=source[:64],
        collector_version=str(raw_data.get("extractor_version", "unknown"))[:32],
        parse_status="success",
        candidate_count=max(0, int(raw_data.get("candidate_count") or len(candidates))),
        source_http_status=200,
        raw_payload={
            "store_url": normalized_page,
            "cursor_before": cursor_before,
            "cursor_after": cursor_result.next_page_url,
            "continuation_saved": cursor_result.last_outcome != "pagination_unknown",
            "candidate_ids": [item.platform_product_id for item in candidates],
            "candidate_diagnostics": diagnostics,
            "products": [
                item.model_dump(mode="json", exclude={"public_cumulative_sold"})
                for item in candidates
            ],
            "raw_data": raw_data,
        },
    )
    db.add(snapshot)
    db.flush()
    metric_dates: set[date] = set()
    for product, candidate, values, rank in observed:
        product_snapshot = save_store_product_snapshot(
            db,
            product,
            captured_at=captured_at,
            sold_count=None,
            title=candidate.title,
            source=source,
            collector_name=source[:64],
            collector_version=str(raw_data.get("extractor_version", "unknown"))[:32],
            raw_payload={
                "collection_mode": "store_page_pool_fill",
                "store_snapshot_id": snapshot.id,
                "rank": rank,
                "product_id": candidate.platform_product_id,
                "url": product.url,
                "title": candidate.title,
                "main_image_url": candidate.image_url,
                "extension_raw_data": raw_data,
            },
            historical_sales=candidate.historical_sales,
            history_target_date=target_date,
            history_source=candidate.history_source,
            commit=False,
        )
        metric_dates.update(
            import_historical_sales(
                db,
                product.id,
                product_snapshot.id,
                candidate.historical_sales,
                target_date=target_date,
                commit=False,
            )
        )
        calculate_product_daily_metrics(db, product.id, commit=False)
    for metric_date in sorted(metric_dates):
        calculate_store_daily_metric(db, store.id, metric_date, commit=False)
    db.commit()
    return {
        "store_id": store.id,
        "discovered_count": len(candidates),
        "added_count": added_count,
        "existing_count": existing_count,
        "deactivated_count": 0,
        "skipped_count": skipped_count,
        "product_links": links,
        "status": "success",
        "error_message": None,
        "continuation_saved": cursor_result.last_outcome != "pagination_unknown",
    }
