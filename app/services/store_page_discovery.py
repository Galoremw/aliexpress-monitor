from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Store, StorePageDiscoveryState


_STORE_PATH = re.compile(r"^/store/(\d+)/pages/all-items\.html$")
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


def _validate_orders_url(store: Store, value: str) -> str:
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
    store_id = _platform_store_id(store)
    if not match or match.group(1) != store_id:
        raise ValueError("店铺 Orders 页面 ID 与监控店铺不一致")
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if [value for key, value in query if key == "shop_sortType"] != ["orders_desc"]:
        raise ValueError("店铺页面必须按 Orders 降序排列")
    return urlunsplit(("https", parsed.netloc.lower(), parsed.path, urlencode(query), ""))


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
    error: str | None = None,
) -> StorePageDiscoveryState:
    state = get_or_create_store_page_discovery_state(db, store)
    try:
        scanned = _validate_orders_url(store, scanned_url)
    except ValueError as exc:
        state.last_outcome = "rejected"
        state.last_error = str(exc)
        db.flush()
        return state

    if scanned != state.next_page_url:
        state.last_outcome = "stale"
        state.last_error = "扫描页面与当前持久化游标不一致"
        db.flush()
        return state

    if not scan_complete:
        state.last_outcome = "incomplete"
        state.last_error = (error or "店铺页面扫描未完成")[:2000]
        db.flush()
        return state

    if has_next_page:
        if not next_page_url:
            state.last_outcome = "rejected"
            state.last_error = "存在下一页但缺少下一页 URL"
            db.flush()
            return state
        try:
            next_url = _validate_orders_url(store, next_page_url)
        except ValueError as exc:
            state.last_outcome = "rejected"
            state.last_error = str(exc)
            db.flush()
            return state
        if next_url == scanned:
            state.last_outcome = "rejected"
            state.last_error = "下一页 URL 与当前页相同"
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
