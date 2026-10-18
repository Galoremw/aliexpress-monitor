from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models import BrowserCollectionItem, BrowserCollectionRun, Store, StoreUrlIdentity
from app.services.store_identity import normalize_store_orders_identity


def test_normalize_store_orders_identity_ignores_query_and_fragment():
    route_id, identity = normalize_store_orders_identity(
        "https://WWW.ALIEXPRESS.COM/store/1105179516/pages/all-items.html"
        "?sortType=bestmatch_sort&page=2#products"
    )

    assert route_id == "1105179516"
    assert identity == "www.aliexpress.com/store/1105179516/pages/all-items.html"


def test_normalize_store_orders_identity_preserves_distinct_route_ids():
    first = normalize_store_orders_identity(
        "https://www.aliexpress.com/store/1105179516/pages/all-items.html"
    )
    second = normalize_store_orders_identity(
        "https://www.aliexpress.com/store/1105182474/pages/all-items.html"
    )

    assert first == (
        "1105179516",
        "www.aliexpress.com/store/1105179516/pages/all-items.html",
    )
    assert second == (
        "1105182474",
        "www.aliexpress.com/store/1105182474/pages/all-items.html",
    )


@pytest.mark.parametrize(
    "page_url",
    [
        "not a url",
        "http://www.aliexpress.com/store/1105179516/pages/all-items.html",
        "https://aliexpress.com.evil.example/store/1105179516/pages/all-items.html",
        "https://www.aliexpress.com/store/1105179516/item/100500.html",
        "https://www.aliexpress.com/store/not-a-number/pages/all-items.html",
        "https://user@www.aliexpress.com/store/1105179516/pages/all-items.html",
    ],
)
def test_normalize_store_orders_identity_rejects_unsafe_urls(page_url: str):
    with pytest.raises(ValueError):
        normalize_store_orders_identity(page_url)


def test_store_url_identity_cannot_bind_route_to_multiple_stores(db_session):
    first_store = Store(
        aliexpress_store_id="1105179516",
        name="First Store",
        url="https://www.aliexpress.com/store/1105179516",
    )
    second_store = Store(
        aliexpress_store_id="1105182474",
        name="Second Store",
        url="https://www.aliexpress.com/store/1105182474",
    )
    db_session.add_all([first_store, second_store])
    db_session.flush()
    db_session.add(
        StoreUrlIdentity(
            store_id=first_store.id,
            route_store_id="1105179516",
            normalized_page_identity=(
                "www.aliexpress.com/store/1105179516/pages/all-items.html"
            ),
            first_observed_url=(
                "https://www.aliexpress.com/store/1105179516/pages/all-items.html"
            ),
            evidence={},
        )
    )
    db_session.commit()

    db_session.add(
        StoreUrlIdentity(
            store_id=second_store.id,
            route_store_id="1105179516",
            normalized_page_identity=(
                "www.aliexpress.com/store/1105179516/pages/all-items.html"
            ),
            first_observed_url=(
                "https://www.aliexpress.com/store/1105179516/pages/all-items.html"
            ),
            evidence={},
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_pending_identity_evidence_does_not_rewrite_canonical_store(db_session):
    store = Store(
        aliexpress_store_id="1105179516",
        name="AMZSTEM Pop Toys Store",
        url="https://www.aliexpress.com/store/1105179516",
    )
    run = BrowserCollectionRun(target_date=date(2026, 10, 18), trigger="MANUAL")
    db_session.add_all([store, run])
    db_session.flush()
    item = BrowserCollectionItem(
        run_id=run.id,
        target_type="STORE",
        target_key="store:1105179516",
        store_id=store.id,
        target_url=store.url,
        position=1,
        identity_review_url=(
            "https://www.aliexpress.com/store/1105182474/pages/all-items.html"
        ),
        identity_review_evidence={"route_store_id": "1105182474"},
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)
    db_session.refresh(store)

    assert item.identity_review_url.endswith("/1105182474/pages/all-items.html")
    assert item.identity_review_evidence == {"route_store_id": "1105182474"}
    assert store.aliexpress_store_id == "1105179516"
    assert store.url == "https://www.aliexpress.com/store/1105179516"
