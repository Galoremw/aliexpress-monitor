from urllib.parse import parse_qs, urlsplit

import pytest

from app.db.models import Store, StorePageDiscoveryState
from app.services.store_page_discovery import (
    advance_store_page_cursor,
    get_or_create_store_page_discovery_state,
)


def _store(db_session, store_id: str = "770700") -> Store:
    store = Store(
        aliexpress_store_id=store_id,
        name=f"Store {store_id}",
        url=f"https://www.aliexpress.com/store/{store_id}?spm=store-test",
    )
    db_session.add(store)
    db_session.flush()
    return store


def test_cursor_initializes_from_canonical_orders_url(db_session):
    store = _store(db_session)

    state = get_or_create_store_page_discovery_state(db_session, store)

    parsed = urlsplit(state.first_orders_url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "www.aliexpress.com"
    assert parsed.path == f"/store/{store.aliexpress_store_id}/pages/all-items.html"
    assert parse_qs(parsed.query)["shop_sortType"] == ["orders_desc"]
    assert state.next_page_url == state.first_orders_url
    assert state.store_id == store.id


def test_complete_page_advances_to_next_url(db_session):
    store = _store(db_session)
    state = get_or_create_store_page_discovery_state(db_session, store)
    scanned_url = state.next_page_url
    next_url = (
        f"https://www.aliexpress.com/store/{store.aliexpress_store_id}/pages/all-items.html"
        "?page=2&sortType=bestmatch_sort&shop_sortType=orders_desc"
    )

    advanced = advance_store_page_cursor(
        db_session,
        store,
        scanned_url=scanned_url,
        has_next_page=True,
        next_page_url=next_url,
        scan_complete=True,
    )

    assert advanced.next_page_url == next_url
    assert advanced.last_successful_page_url == scanned_url


def test_repeated_last_successful_page_does_not_move_cursor_back(db_session):
    store = _store(db_session)
    state = get_or_create_store_page_discovery_state(db_session, store)
    first_url = state.next_page_url
    second_url = (
        f"https://www.aliexpress.com/store/{store.aliexpress_store_id}/pages/all-items.html"
        "?page=2&sortType=bestmatch_sort&shop_sortType=orders_desc"
    )
    advance_store_page_cursor(
        db_session,
        store,
        scanned_url=first_url,
        has_next_page=True,
        next_page_url=second_url,
        scan_complete=True,
    )

    duplicate = advance_store_page_cursor(
        db_session,
        store,
        scanned_url=first_url,
        has_next_page=True,
        next_page_url=second_url,
        scan_complete=True,
    )

    assert duplicate.next_page_url == second_url
    assert duplicate.last_outcome == "duplicate"


def test_confirmed_last_page_wraps_to_first_url(db_session):
    store = _store(db_session)
    state = get_or_create_store_page_discovery_state(db_session, store)
    last_url = (
        f"https://www.aliexpress.com/store/{store.aliexpress_store_id}/pages/all-items.html"
        "?page=4&shop_sortType=orders_desc"
    )
    state.next_page_url = last_url
    db_session.flush()

    advanced = advance_store_page_cursor(
        db_session,
        store,
        scanned_url=last_url,
        has_next_page=False,
        next_page_url=None,
        scan_complete=True,
    )

    assert advanced.next_page_url == state.first_orders_url
    assert advanced.last_successful_page_url == last_url


@pytest.mark.parametrize(
    "scanned_url",
    [
        "https://www.aliexpress.com/store/999999/pages/all-items.html?shop_sortType=orders_desc",
        "https://www.aliexpress.com/store/770700/pages/all-items.html?page=2&shop_sortType=orders_desc",
    ],
)
def test_invalid_or_stale_store_page_does_not_advance_cursor(db_session, scanned_url):
    store = _store(db_session)
    state = get_or_create_store_page_discovery_state(db_session, store)
    original_cursor = state.next_page_url

    result = advance_store_page_cursor(
        db_session,
        store,
        scanned_url=scanned_url,
        has_next_page=True,
        next_page_url=(
            f"https://www.aliexpress.com/store/{store.aliexpress_store_id}/pages/all-items.html"
            "?page=3&shop_sortType=orders_desc"
        ),
        scan_complete=True,
    )

    assert result.next_page_url == original_cursor


def test_incomplete_page_retains_cursor(db_session):
    store = _store(db_session)
    state = get_or_create_store_page_discovery_state(db_session, store)
    original_cursor = state.next_page_url

    result = advance_store_page_cursor(
        db_session,
        store,
        scanned_url=original_cursor,
        has_next_page=True,
        next_page_url=(
            f"https://www.aliexpress.com/store/{store.aliexpress_store_id}/pages/all-items.html"
            "?page=2&shop_sortType=orders_desc"
        ),
        scan_complete=False,
        error="page_not_ready",
    )

    assert result.next_page_url == original_cursor
