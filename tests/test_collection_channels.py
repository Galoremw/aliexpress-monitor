from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.collectors.base import CollectorResult
from app.collectors.dependencies import get_collector
from app.db.models import (
    CollectionAttempt,
    ManualCollectionTask,
    Product,
    ProductSnapshot,
    StorePageDiscoveryState,
    StoreSnapshot,
)


def create_product(client, product_id="100500700"):
    store = client.post(
        "/api/stores",
        json={"name": "Channel Store", "url": "https://www.aliexpress.com/store/770700"},
    ).json()
    product = client.post(
        "/api/products",
        json={"store_id": store["id"], "aliexpress_product_id": product_id},
    ).json()
    return store, product


def extension_payload(product_id="100500700", sold_count=88):
    return {
        "platform_product_id": product_id,
        "url": f"https://www.aliexpress.com/item/{product_id}.html",
        "title": "Extension Product 11 sold HK$ 12.50",
        "sold_count": sold_count,
        "price": "12.50",
        "rating": "4.8",
        "review_count": 321,
        "captured_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "raw_data": {"extractor_version": "extension-test"},
    }


def _ranked_page_candidate(store_id: str, index: int, *, values=None, complete=True):
    product_id = f"10050199{store_id[-3:]}{index:03d}"
    dates = [f"2026-10-{day:02d}" for day in range(5, 12)]
    values = list(values if values is not None else range(index + 1, index + 8))
    points = [
        {"date": day, "value": value, "value_type": "daily_increment"}
        for day, value in zip(dates, values)
    ]
    if not complete:
        points.pop()
    return {
        "platform_product_id": product_id,
        "url": f"https://www.aliexpress.com/item/{product_id}.html",
        "title": f"Candidate {index}",
        "history_source": "ixspy_visible_daily_increment",
        "history_daily_capture_complete": True,
        "history_expected_dates": dates,
        "history_missing_dates": [] if complete else [dates[-1]],
        "historical_sales": points,
    }


def _ranked_page_payload(store_id: str, candidates, *, page=1, has_next_page=True):
    next_page_url = None
    if has_next_page:
        next_page_url = (
            f"https://www.aliexpress.com/store/{store_id}/pages/all-items.html"
            f"?page={page + 1}&sortType=bestmatch_sort&shop_sortType=orders_desc"
        )
    return {
        "platform_store_id": store_id,
        "url": (
            f"https://www.aliexpress.com/store/{store_id}/pages/all-items.html"
            f"?page={page}&sortType=bestmatch_sort&shop_sortType=orders_desc"
        ),
        "products": candidates,
        "raw_data": {
            "extractor_version": "extension-store-selection-test",
            "collection_mode": "top_7d_increment_selection",
            "selection_method": "ixspy_visible_latest_7_day_increment_sum",
            "order_sort_verified": True,
            "scan_complete": True,
            "pagination_known": True,
            "has_next_page": has_next_page,
            "next_page_url": next_page_url,
            "candidate_count": 30,
            "unique_candidate_count": 30,
            "history_target_date": "2026-10-12",
            "selection": [
                {
                    "product_id": candidate["platform_product_id"],
                    "orders_rank": index + 1,
                }
                for index, candidate in enumerate(candidates)
            ],
        },
    }


def test_browser_extension_creates_channel_snapshot_without_overwriting(client, db_session):
    _, product = create_product(client)

    first = client.post("/api/collection/browser-extension", json=extension_payload())
    second = client.post(
        "/api/collection/browser-extension",
        json=extension_payload(sold_count=91),
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["source"] == "CHROME_EXTENSION"
    assert first.json()["status"] == "SUSPECT"
    assert first.json()["sold_count"] is None
    assert second.json()["sold_count"] is None
    snapshots = list(db_session.query(ProductSnapshot).filter_by(product_id=product["id"]))
    assert len(snapshots) == 2
    assert snapshots[0].raw_data["extractor_version"] == "extension-test"
    assert snapshots[0].raw_data["historical_sales"] == []
    assert snapshots[0].title == "Extension Product"
    assert db_session.get(Product, product["id"]).title == "Extension Product"


def test_browser_extension_rejects_unmonitored_product(client):
    response = client.post(
        "/api/collection/browser-extension",
        json=extension_payload(product_id="100500799"),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "该商品尚未加入监控"


def test_browser_extension_rejects_deactivated_product(client):
    _, product = create_product(client, product_id="100500798")
    response = client.post(f"/api/products/{product['id']}/deactivate")
    assert response.status_code == 200

    response = client.post(
        "/api/collection/browser-extension",
        json=extension_payload(product_id="100500798"),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "该商品尚未加入监控"


def test_browser_extension_matches_monitored_url_when_page_id_is_redirected(client):
    _, product = create_product(client, product_id="100500705")
    payload = extension_payload(product_id="3256810400878106")
    payload["url"] = product["url"]

    response = client.post("/api/collection/browser-extension", json=payload)

    assert response.status_code == 201
    assert response.json()["product_id"] == product["id"]


def test_browser_extension_matches_navigation_alias_when_page_redirects(client):
    _, product = create_product(client, product_id="3256812061169120")
    payload = extension_payload(product_id="1005012247483872")
    payload["raw_data"]["monitored_product_id"] = "3256812061169120"

    response = client.post("/api/collection/browser-extension", json=payload)

    assert response.status_code == 201
    assert response.json()["product_id"] == product["id"]


def test_browser_extension_store_discovery_adds_top_twenty_links(client, db_session):
    store = client.post(
        "/api/stores",
        json={"name": "Store Page", "url": "https://www.aliexpress.com/store/770701"},
    ).json()
    payload = {
        "platform_store_id": "770701",
        "url": "https://www.aliexpress.com/store/770701",
        "products": [
            {
                "platform_product_id": f"1005008{index:04d}",
                "url": f"https://www.aliexpress.com/item/1005008{index:04d}.html",
                "title": (
                    f"Store Product {index} 1,200 sold HK$ 4.25"
                    if index == 0
                    else f"Store Product {index}"
                ),
                "public_cumulative_sold": 1000 - index,
                "image_url": f"https://ae01.alicdn.com/kf/{index}.jpg",
            }
            for index in range(20)
        ],
        "raw_data": {"extractor_version": "extension-store-test"},
    }

    response = client.post("/api/collection/browser-extension/store", json=payload)

    assert response.status_code == 201
    assert response.json()["added_count"] == 20
    products = list(db_session.query(Product).filter_by(store_id=store["id"]))
    assert len(products) == 20
    assert products[0].discovery_source == "chrome_extension_store"
    assert products[0].title == "Store Product 0"
    snapshots = list(
        db_session.query(ProductSnapshot).filter(ProductSnapshot.source == "CHROME_EXTENSION")
    )
    assert len(snapshots) == 20
    assert all(snapshot.sold_count is None for snapshot in snapshots)
    assert {
        snapshot.raw_data["main_image_url"] for snapshot in snapshots
    } == {f"https://ae01.alicdn.com/kf/{index}.jpg" for index in range(20)}


def test_ranked_store_enrollment_preserves_overfilled_pool(client, db_session):
    store = client.post(
        "/api/stores",
        json={"name": "Ranked Store", "url": "https://www.aliexpress.com/store/770708"},
    ).json()
    products = []
    for index in range(21):
        product_id = f"10050077070{index:02d}"
        product = Product(
            store_id=store["id"],
            aliexpress_product_id=product_id,
            url=f"https://www.aliexpress.com/item/{product_id}.html",
            discovery_source="chrome_extension_store",
            discovery_rank=index + 1,
        )
        db_session.add(product)
        products.append(product)
    db_session.commit()

    payload = {
        "platform_store_id": "770708",
        "url": "https://www.aliexpress.com/store/770708/pages/all-items.html?shop_sortType=orders_desc",
        "products": [
            {
                "platform_product_id": item.aliexpress_product_id,
                "url": item.url,
            }
            for item in products[:19]
        ],
        "raw_data": {
            "collection_mode": "top_7d_increment_selection",
            "order_sort_verified": True,
            "scan_complete": True,
            "pagination_known": True,
            "has_next_page": False,
            "next_page_url": None,
            "history_target_date": "2026-10-12",
            "candidate_count": 21,
            "unique_candidate_count": 21,
        },
    }

    response = client.post("/api/collection/browser-extension/store", json=payload)

    assert response.status_code == 201, response.text
    assert response.json()["added_count"] == 0
    assert response.json()["deactivated_count"] == 0
    assert all(item.status == "active" for item in products)


def test_ranked_store_enrollment_skips_incomplete_histories_even_when_flagged_complete(client, db_session):
    store = client.post(
        "/api/stores",
        json={"name": "Incomplete Ranked Store", "url": "https://www.aliexpress.com/store/770712"},
    ).json()
    products = []
    expected_dates = [
            "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",
            "2026-10-09", "2026-10-10", "2026-10-11",
    ]
    for index in range(20):
        product_id = f"10050077074{index:02d}"
        product = Product(
            store_id=store["id"],
            aliexpress_product_id=product_id,
            url=f"https://www.aliexpress.com/item/{product_id}.html",
            discovery_source="chrome_extension_store",
            discovery_rank=index + 1,
        )
        db_session.add(product)
        products.append(product)
    db_session.commit()

    selected = []
    for index, product in enumerate(products):
        complete = index < 19
        history_dates = expected_dates
        if index == 18:
            history_dates = [
                "2026-10-05", "2026-10-06", "2026-10-08", "2026-10-09",
                "2026-10-10", "2026-10-11", "2026-10-12",
            ]
        selected.append({
            "platform_product_id": product.aliexpress_product_id,
            "url": product.url,
            "history_source": "ixspy_visible_daily_increment",
            "history_daily_capture_complete": True,
            "history_expected_dates": history_dates,
            "history_missing_dates": [] if complete else [history_dates[-1]],
            "historical_sales": [
                {"date": day, "value": index, "value_type": "daily_increment"}
                for day in (history_dates if complete else history_dates[:-1])
            ],
        })
    response = client.post(
        "/api/collection/browser-extension/store",
        json={
            "platform_store_id": "770712",
            "url": "https://www.aliexpress.com/store/770712/pages/all-items.html?shop_sortType=orders_desc",
            "products": selected,
            "raw_data": {
                "collection_mode": "top_7d_increment_selection",
                "order_sort_verified": True,
                "scan_complete": True,
                "pagination_known": True,
                "has_next_page": False,
                "next_page_url": None,
                "history_target_date": "2026-10-12",
                "candidate_count": 20,
                "selection_method": "ixspy_visible_latest_7_day_increment_sum",
                "unique_candidate_count": 20,
            },
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["added_count"] == 0
    assert response.json()["existing_count"] == 18
    assert response.json()["skipped_count"] == 2
    assert all(item.status == "active" for item in products)
    assert db_session.query(ProductSnapshot).filter(
        ProductSnapshot.product_id.in_([item.id for item in products])
    ).count() == 18
    assert db_session.query(StoreSnapshot).filter_by(store_id=store["id"]).count() == 1


def test_ranked_store_enrollment_uses_current_candidate_count_not_inactive_history(client, db_session):
    store = client.post(
        "/api/stores",
        json={"name": "Small Ranked Store", "url": "https://www.aliexpress.com/store/770710"},
    ).json()
    for index in range(51):
        product_id = f"10050077072{index:02d}"
        db_session.add(
            Product(
                store_id=store["id"],
                aliexpress_product_id=product_id,
                url=f"https://www.aliexpress.com/item/{product_id}.html",
                status="inactive",
                discovery_source="chrome_extension_store",
            )
        )
    db_session.commit()

    expected_dates = [
        "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",
        "2026-10-09", "2026-10-10", "2026-10-11",
    ]
    candidates = []
    for index in range(7):
        product_id = f"10050077073{index:02d}"
        candidates.append({
            "platform_product_id": product_id,
            "url": f"https://www.aliexpress.com/item/{product_id}.html",
            "history_source": "ixspy_visible_daily_increment",
            "history_daily_capture_complete": True,
            "history_expected_dates": expected_dates,
            "historical_sales": [
                {"date": day, "value": index, "value_type": "daily_increment"}
                for day in expected_dates
            ],
        })
    response = client.post(
        "/api/collection/browser-extension/store",
        json={
            "platform_store_id": "770710",
            "url": "https://www.aliexpress.com/store/770710/pages/all-items.html?shop_sortType=orders_desc",
            "products": candidates,
            "raw_data": {
                "collection_mode": "top_7d_increment_selection",
                "order_sort_verified": True,
                "scan_complete": True,
                "pagination_known": True,
                "has_next_page": False,
                "next_page_url": None,
                "history_target_date": "2026-10-12",
                "candidate_count": 30,
                "selection_method": "ixspy_visible_latest_7_day_increment_sum",
                "unique_candidate_count": 7,
            },
        },
    )

    assert response.status_code == 201, response.text
    active_products = list(
        db_session.query(Product).filter_by(store_id=store["id"], status="active")
    )
    assert {item.aliexpress_product_id for item in active_products} == {
        item["platform_product_id"] for item in candidates
    }


def test_ranked_store_enrollment_does_not_reactivate_inactive_products(client, db_session):
    store = client.post(
        "/api/stores",
        json={"name": "Re-ranked Store", "url": "https://www.aliexpress.com/store/770720"},
    ).json()
    products_by_rank = {}
    for index in range(51):
        product_id = f"10050077074{index:02d}"
        product = Product(
            store_id=store["id"],
            aliexpress_product_id=product_id,
            url=f"https://www.aliexpress.com/item/{product_id}.html",
            status="inactive",
            discovery_source="chrome_extension_store",
        )
        db_session.add(product)
        products_by_rank[index] = product
    db_session.commit()

    expected_dates = [
        "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",
        "2026-10-09", "2026-10-10", "2026-10-11",
    ]
    selected_ranks = list(range(50, 30, -1))
    selection = []
    for selection_rank, candidate_rank in enumerate(selected_ranks, start=1):
        product = products_by_rank[candidate_rank]
        selection.append({
            "platform_product_id": product.aliexpress_product_id,
            "url": product.url,
            "history_source": "ixspy_visible_daily_increment",
            "history_daily_capture_complete": True,
            "history_expected_dates": expected_dates,
            "history_missing_dates": [],
            "historical_sales": [
                {"date": day, "value": candidate_rank, "value_type": "daily_increment"}
                for day in expected_dates
            ],
            "title": f"Candidate {candidate_rank}",
        })
    response = client.post(
        "/api/collection/browser-extension/store",
        json={
            "platform_store_id": "770720",
            "url": "https://www.aliexpress.com/store/770720/pages/all-items.html?shop_sortType=orders_desc",
            "products": selection,
            "raw_data": {
                "collection_mode": "top_7d_increment_selection",
                "order_sort_verified": True,
                "scan_complete": True,
                "pagination_known": True,
                "has_next_page": True,
                "next_page_url": "https://www.aliexpress.com/store/770720/pages/all-items.html?page=2&shop_sortType=orders_desc",
                "history_target_date": "2026-10-12",
                "candidate_count": 30,
                "selection_method": "ixspy_visible_latest_7_day_increment_sum",
                "unique_candidate_count": 51,
                "selection": [
                    {
                        "product_id": item["platform_product_id"],
                        "selection_rank": rank,
                        "selection_score_7d": selected_ranks[rank - 1] * 7,
                    }
                    for rank, item in enumerate(selection, start=1)
                ],
            },
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["existing_count"] == 0
    assert response.json()["added_count"] == 0
    assert response.json()["skipped_count"] == 20
    assert response.json()["deactivated_count"] == 0
    assert db_session.query(Product).filter_by(store_id=store["id"], status="active").count() == 0
    assert db_session.query(Product).filter_by(store_id=store["id"], status="inactive").count() == 51
    assert db_session.query(ProductSnapshot).count() == 0


def test_ranked_store_enrollment_skips_inactive_candidate_before_filling_vacancy(client, db_session):
    store = client.post(
        "/api/stores",
        json={"name": "Fill Vacancy Store", "url": "https://www.aliexpress.com/store/770722"},
    ).json()
    inactive_id = "10050077072200"
    inactive = Product(
        store_id=store["id"],
        aliexpress_product_id=inactive_id,
        url=f"https://www.aliexpress.com/item/{inactive_id}.html",
        status="inactive",
    )
    db_session.add(inactive)
    db_session.commit()
    expected_dates = [
        "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",
        "2026-10-09", "2026-10-10", "2026-10-11",
    ]
    candidates = []
    for index in range(21):
        product_id = inactive_id if index == 0 else f"100500770722{index:02d}"
        candidates.append({
            "platform_product_id": product_id,
            "url": f"https://www.aliexpress.com/item/{product_id}.html",
            "title": f"Candidate {index}",
            "history_source": "ixspy_visible_daily_increment",
            "history_daily_capture_complete": True,
            "history_expected_dates": expected_dates,
            "history_missing_dates": [],
            "historical_sales": [
                {"date": day, "value": 21 - index, "value_type": "daily_increment"}
                for day in expected_dates
            ],
        })
    response = client.post(
        "/api/collection/browser-extension/store",
        json={
            "platform_store_id": "770722",
            "url": "https://www.aliexpress.com/store/770722/pages/all-items.html?shop_sortType=orders_desc",
            "products": candidates,
            "raw_data": {
                "collection_mode": "top_7d_increment_selection",
                "order_sort_verified": True,
                "scan_complete": True,
                "pagination_known": True,
                "has_next_page": False,
                "next_page_url": None,
                "history_target_date": "2026-10-12",
                "candidate_count": 21,
                "selection": [
                    {"product_id": item["platform_product_id"], "orders_rank": index + 1}
                    for index, item in enumerate(candidates)
                ],
            },
        },
    )

    assert response.status_code == 201, response.text
    active_ids = {
        product.aliexpress_product_id
        for product in db_session.query(Product).filter_by(store_id=store["id"], status="active")
    }
    assert len(active_ids) == 20
    assert inactive_id not in active_ids
    assert candidates[-1]["platform_product_id"] in active_ids


def test_manual_store_page_enrolls_products_but_does_not_wrap_when_pagination_is_unknown(client, db_session):
    store = client.post(
        "/api/stores",
        json={"name": "Unknown Pagination Store", "url": "https://www.aliexpress.com/store/770723"},
    ).json()
    candidate = _ranked_page_candidate("770723", 1)
    payload = _ranked_page_payload("770723", [candidate], has_next_page=False)
    payload["raw_data"]["pagination_known"] = False

    response = client.post("/api/collection/browser-extension/store", json=payload)

    assert response.status_code == 201, response.text
    assert response.json()["added_count"] == 1
    assert response.json()["continuation_saved"] is False
    state = db_session.get(StorePageDiscoveryState, store["id"])
    assert state.next_page_url == state.first_orders_url
    assert state.last_outcome == "pagination_unknown"


def test_ranked_store_enrollment_never_deactivates_unselected_products(client, db_session):
    store = client.post(
        "/api/stores",
        json={"name": "Ranked Store", "url": "https://www.aliexpress.com/store/770709"},
    ).json()
    products = []
    for index in range(21):
        product_id = f"10050077071{index:02d}"
        product = Product(
            store_id=store["id"],
            aliexpress_product_id=product_id,
            url=f"https://www.aliexpress.com/item/{product_id}.html",
            discovery_source="chrome_extension_store",
            discovery_rank=index + 1,
        )
        db_session.add(product)
        products.append(product)
    db_session.commit()
    historical = client.post(
        "/api/collection/browser-extension",
        json=extension_payload(product_id=products[-1].aliexpress_product_id),
    )
    assert historical.status_code == 201

    selection = [
        {
            "platform_product_id": item.aliexpress_product_id,
            "url": item.url,
            "history_source": "ixspy_visible_daily_increment",
            "history_daily_capture_complete": True,
            "history_expected_dates": ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"],
            "historical_sales": [
                    {"date": day, "value": index, "value_type": "daily_increment"}
                for day, index in zip(
                    ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"],
                    range(7),
                )
            ],
        }
        for item in products[:20]
    ]
    payload = {
        "platform_store_id": "770709",
        "url": "https://www.aliexpress.com/store/770709/pages/all-items.html?shop_sortType=orders_desc",
        "products": selection,
        "raw_data": {
            "collection_mode": "top_7d_increment_selection",
            "order_sort_verified": True,
            "scan_complete": True,
            "pagination_known": True,
            "has_next_page": False,
            "next_page_url": None,
            "history_target_date": "2026-10-12",
            "candidate_count": 30,
            "unique_candidate_count": 21,
            "selection": [
                {"product_id": item["platform_product_id"], "selection_rank": index + 1, "selection_reason": "latest_7d_increment"}
                for index, item in enumerate(selection)
            ],
        },
    }

    response = client.post("/api/collection/browser-extension/store", json=payload)

    assert response.status_code == 201, response.text
    assert response.json()["deactivated_count"] == 0
    assert all(item.status == "active" for item in products[:20])
    assert products[-1].status == "active"
    assert db_session.query(ProductSnapshot).filter_by(product_id=products[-1].id).count() == 1
    store_snapshot = db_session.query(StoreSnapshot).filter_by(store_id=store["id"]).one()
    assert store_snapshot.raw_payload["raw_data"]["selection"][0]["selection_rank"] == 1


def test_ranked_store_enrollment_preserves_mixed_source_pool_members(client, db_session):
    store = client.post(
        "/api/stores",
        json={"name": "Mixed Source Store", "url": "https://www.aliexpress.com/store/770721"},
    ).json()
    products = []
    for index in range(22):
        product_id = f"10050077075{index:02d}"
        product = Product(
            store_id=store["id"],
            aliexpress_product_id=product_id,
            url=f"https://www.aliexpress.com/item/{product_id}.html",
            discovery_source="manual" if index in (19, 20) else "chrome_extension_store",
            status="inactive" if index == 19 else "active",
        )
        db_session.add(product)
        products.append(product)
    db_session.commit()

    expected_dates = [
        "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",
        "2026-10-09", "2026-10-10", "2026-10-11",
    ]
    selected = [
        {
            "platform_product_id": product.aliexpress_product_id,
            "url": product.url,
            "history_source": "ixspy_visible_daily_increment",
            "history_daily_capture_complete": True,
            "history_expected_dates": expected_dates,
            "history_missing_dates": [],
            "historical_sales": [
                {"date": day, "value": 20 - index, "value_type": "daily_increment"}
                for day in expected_dates
            ],
        }
        for index, product in enumerate(products[:20])
    ]
    response = client.post(
        "/api/collection/browser-extension/store",
        json={
            "platform_store_id": "770721",
            "url": "https://www.aliexpress.com/store/770721/pages/all-items.html?shop_sortType=orders_desc",
            "products": selected,
            "raw_data": {
                "collection_mode": "top_7d_increment_selection",
                "order_sort_verified": True,
                "scan_complete": True,
                "pagination_known": True,
                "has_next_page": False,
                "next_page_url": None,
                "history_target_date": "2026-10-12",
                "candidate_count": 30,
                "unique_candidate_count": 22,
            },
        },
    )

    assert response.status_code == 201, response.text
    active_products = db_session.query(Product).filter_by(store_id=store["id"], status="active").all()
    assert len(active_products) == 21
    assert products[19].status == "inactive"
    assert products[20].status == "active"
    assert products[21].status == "active"


def test_ranked_store_enrollment_rejects_conflicting_candidate_without_partial_pool(client, db_session):
    store = client.post(
        "/api/stores",
        json={"name": "Target Ranked Store", "url": "https://www.aliexpress.com/store/770717"},
    ).json()
    conflicting_store = client.post(
        "/api/stores",
        json={"name": "Other Store", "url": "https://www.aliexpress.com/store/770718"},
    ).json()
    historical_products = [
        Product(
            store_id=store["id"],
            aliexpress_product_id=f"100500771{index:03d}",
            url=f"https://www.aliexpress.com/item/100500771{index:03d}.html",
            status="inactive",
            discovery_source="chrome_extension_store",
        )
        for index in range(51)
    ]
    conflicting_id = "100500772019"
    db_session.add_all(historical_products)
    db_session.add(
        Product(
            store_id=conflicting_store["id"],
            aliexpress_product_id=conflicting_id,
            url=f"https://www.aliexpress.com/item/{conflicting_id}.html",
            discovery_source="manual",
        )
    )
    db_session.commit()

    expected_dates = [
            "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",
            "2026-10-09", "2026-10-10", "2026-10-11",
    ]
    candidates = []
    for index in range(20):
        product_id = conflicting_id if index == 19 else f"100500772{index:03d}"
        candidates.append({
            "platform_product_id": product_id,
            "url": f"https://www.aliexpress.com/item/{product_id}.html",
            "history_source": "ixspy_visible_daily_increment",
            "history_daily_capture_complete": True,
            "history_expected_dates": expected_dates,
            "history_missing_dates": [],
            "historical_sales": [
                {"date": day, "value": index, "value_type": "daily_increment"}
                for day in expected_dates
            ],
        })
    response = client.post(
        "/api/collection/browser-extension/store",
        json={
            "platform_store_id": "770717",
            "url": "https://www.aliexpress.com/store/770717/pages/all-items.html?shop_sortType=orders_desc",
            "products": candidates,
            "raw_data": {
                "collection_mode": "top_7d_increment_selection",
                "order_sort_verified": True,
                "scan_complete": True,
                "pagination_known": True,
                "has_next_page": False,
                "next_page_url": None,
                "history_target_date": "2026-10-12",
                "candidate_count": 30,
                "selection_method": "ixspy_visible_latest_7_day_increment_sum",
                "unique_candidate_count": 20,
            },
        },
    )

    assert response.status_code == 409
    assert "其他监控店铺" in response.json()["detail"]
    assert db_session.query(Product).filter_by(store_id=store["id"]).count() == 51
    assert all(item.status == "inactive" for item in historical_products)
    assert db_session.query(StoreSnapshot).filter_by(store_id=store["id"]).count() == 0
    assert db_session.query(ProductSnapshot).count() == 0


def test_ranked_store_enrollment_rejects_cumulative_like_daily_history(client, db_session):
    store = client.post(
        "/api/stores",
        json={"name": "Suspicious History Store", "url": "https://www.aliexpress.com/store/770725"},
    ).json()
    values = [26170, 26213, 26257, 26245, 26202, 26262, 26263]
    dates = [
        "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
        "2026-10-05", "2026-10-06", "2026-10-07",
    ]
    response = client.post(
        "/api/collection/browser-extension/store",
        json={
            "platform_store_id": "770725",
            "url": "https://www.aliexpress.com/store/770725/pages/all-items.html?shop_sortType=orders_desc",
            "products": [{
                "platform_product_id": "1005007707250",
                "url": "https://www.aliexpress.com/item/1005007707250.html",
                "history_source": "ixspy_visible_daily_increment",
                "history_daily_capture_complete": True,
                "history_expected_dates": dates,
                "history_missing_dates": [],
                "historical_sales": [
                    {"date": day, "value": value, "value_type": "daily_increment"}
                    for day, value in zip(dates, values)
                ],
            }],
            "raw_data": {
                "collection_mode": "top_7d_increment_selection",
                "order_sort_verified": True,
                "scan_complete": True,
                "pagination_known": True,
                "has_next_page": False,
                "next_page_url": None,
                "history_target_date": "2026-10-08",
                "candidate_count": 1,
                "unique_candidate_count": 1,
            },
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["added_count"] == 0
    assert response.json()["skipped_count"] == 1
    assert db_session.query(Product).filter_by(store_id=store["id"]).count() == 0
    assert db_session.query(StoreSnapshot).filter_by(store_id=store["id"]).count() == 1


def test_product_without_store_is_added_to_custom_monitoring_store(client):
    response = client.post(
        "/api/products",
        json={"url": "https://www.aliexpress.com/item/100500799001.html"},
    )

    assert response.status_code == 201
    product = response.json()
    assert product["store_id"] > 0
    stores = client.get("/api/stores").json()
    custom_stores = [store for store in stores if store["name"] == "自定义监控"]
    assert len(custom_stores) == 1
    assert product["store_id"] == custom_stores[0]["id"]

    second = client.post(
        "/api/products",
        json={"url": "https://www.aliexpress.com/item/100500799002.html"},
    )
    assert second.status_code == 201
    assert second.json()["store_id"] == product["store_id"]


def test_auto_failure_creates_attempt_and_manual_task(client, db_session):
    _, product = create_product(client, product_id="100500702")

    class FailedCollector:
        name = "failed_fixture"
        version = "1"

        def collect(self, target):
            return CollectorResult(
                collected_at=datetime(2026, 9, 21, 2, tzinfo=timezone.utc),
                collector_name=self.name,
                collector_version=self.version,
                parse_status="failed",
                error_type="platform_challenge",
                error_message="verification page",
            )

    client.app.dependency_overrides[get_collector] = lambda: FailedCollector()
    response = client.post("/api/jobs/collect-now")

    assert response.status_code == 200
    attempt = db_session.query(CollectionAttempt).filter_by(product_id=product["id"]).one()
    task = db_session.query(ManualCollectionTask).filter_by(product_id=product["id"]).one()
    assert attempt.source == "AUTO"
    assert attempt.status == "FAILED"
    assert task.status == "PENDING"
    assert client.get("/api/collection/pending").json()[0]["product_id"] == product["id"]


def test_manual_extension_completion_closes_pending_task(client, db_session):
    _, product = create_product(client, product_id="100500703")
    task = ManualCollectionTask(
        product_id=product["id"],
        reason="platform_challenge",
        status="PENDING",
    )
    db_session.add(task)
    db_session.commit()

    response = client.post(
        "/api/collection/browser-extension",
        json=extension_payload(product_id="100500703", sold_count=5),
    )

    assert response.status_code == 201
    db_session.refresh(task)
    assert task.status == "COMPLETED"
    assert client.get("/api/collection/pending").json() == []


def test_collection_status_today_exposes_auto_and_manual_counts(client):
    create_product(client, product_id="100500704")
    response = client.get("/api/collection/status/today")

    assert response.status_code == 200
    body = response.json()
    assert body["total_products"] == 1
    assert set(body) == {
        "date",
        "total_products",
        "auto_success",
        "auto_failed",
        "manual_completed",
        "pending_manual",
        "success_rate",
    }


def test_collection_progress_tracks_manual_snapshot_and_next_product(client, db_session):
    store = client.post(
        "/api/stores",
        json={"name": "Progress Store", "url": "https://www.aliexpress.com/store/770702"},
    ).json()
    first = client.post(
        "/api/products",
        json={"store_id": store["id"], "aliexpress_product_id": "100500710"},
    ).json()
    second = client.post(
        "/api/products",
        json={"store_id": store["id"], "aliexpress_product_id": "100500711"},
    ).json()

    response = client.post(
        "/api/collection/browser-extension",
        json=extension_payload(product_id=first["aliexpress_product_id"], sold_count=22),
    )

    assert response.status_code == 201
    progress = client.get("/api/collection/progress")
    assert progress.status_code == 200
    body = progress.json()
    assert body["total"] == 2
    assert body["completed"] == 1
    assert body["pending"] == 1
    assert body["next_product"]["product_id"] == second["id"]
    assert body["items"][0]["status"] == "completed"

    status = client.get("/api/collection/status/today")
    assert status.status_code == 200
    assert status.json()["manual_completed"] == 1


def test_ranked_selection_enrolls_fewer_than_twenty_complete_candidates(client, db_session):
    store_id = "770730"
    store = client.post(
        "/api/stores",
        json={"name": "Pagewise Store", "url": f"https://www.aliexpress.com/store/{store_id}"},
    ).json()
    candidates = [_ranked_page_candidate(store_id, index) for index in range(3)]

    response = client.post(
        "/api/collection/browser-extension/store",
        json=_ranked_page_payload(store_id, candidates),
    )

    assert response.status_code == 201, response.text
    assert response.json()["added_count"] == 3
    assert response.json()["deactivated_count"] == 0
    assert db_session.query(Product).filter_by(store_id=store["id"], status="active").count() == 3
    state = db_session.get(StorePageDiscoveryState, store["id"])
    assert "page=2" in state.next_page_url


def test_zero_eligible_page_is_recorded_and_cursor_saved(client, db_session):
    store_id = "770731"
    store = client.post(
        "/api/stores",
        json={"name": "No Eligible Page", "url": f"https://www.aliexpress.com/store/{store_id}"},
    ).json()
    payload = _ranked_page_payload(store_id, [])

    response = client.post("/api/collection/browser-extension/store", json=payload)

    assert response.status_code == 201, response.text
    assert response.json()["added_count"] == 0
    assert response.json()["continuation_saved"] is True
    snapshot = db_session.query(StoreSnapshot).filter_by(store_id=store["id"]).one()
    assert snapshot.parse_status == "success"
    state = db_session.get(StorePageDiscoveryState, store["id"])
    assert "page=2" in state.next_page_url


def test_repeated_submission_and_overfilled_pool_are_append_only(client, db_session):
    store_id = "770732"
    store = client.post(
        "/api/stores",
        json={"name": "Repeated Page Store", "url": f"https://www.aliexpress.com/store/{store_id}"},
    ).json()
    first = _ranked_page_candidate(store_id, 0)
    second = _ranked_page_candidate(store_id, 1)
    first_payload = _ranked_page_payload(store_id, [first])
    first_response = client.post("/api/collection/browser-extension/store", json=first_payload)
    assert first_response.status_code == 201, first_response.text

    repeated_payload = _ranked_page_payload(store_id, [first, second])
    repeated_response = client.post("/api/collection/browser-extension/store", json=repeated_payload)

    assert repeated_response.status_code == 201, repeated_response.text
    assert repeated_response.json()["added_count"] == 1
    assert repeated_response.json()["existing_count"] == 1
    assert db_session.query(Product).filter_by(store_id=store["id"], status="active").count() == 2
    state = db_session.get(StorePageDiscoveryState, store["id"])
    assert "page=2" in state.next_page_url

    overfilled_store_id = "770733"
    overfilled_store = client.post(
        "/api/stores",
        json={
            "name": "Legacy Overfilled Store",
            "url": f"https://www.aliexpress.com/store/{overfilled_store_id}",
        },
    ).json()
    existing = [
        Product(
            store_id=overfilled_store["id"],
            aliexpress_product_id=f"10050199800{index:02d}",
            url=f"https://www.aliexpress.com/item/10050199800{index:02d}.html",
            discovery_source="manual",
        )
        for index in range(21)
    ]
    db_session.add_all(existing)
    db_session.commit()

    overfilled_response = client.post(
        "/api/collection/browser-extension/store",
        json=_ranked_page_payload(
            overfilled_store_id, [_ranked_page_candidate(overfilled_store_id, 99)]
        ),
    )

    assert overfilled_response.status_code == 201, overfilled_response.text
    assert overfilled_response.json()["added_count"] == 0
    assert overfilled_response.json()["deactivated_count"] == 0
    assert db_session.query(Product).filter_by(store_id=overfilled_store["id"], status="active").count() == 21


def test_incomplete_or_cumulative_candidates_do_not_mutate_pool(client, db_session):
    store_id = "770734"
    store = client.post(
        "/api/stores",
        json={"name": "Invalid Candidate Page", "url": f"https://www.aliexpress.com/store/{store_id}"},
    ).json()
    incomplete = _ranked_page_candidate(store_id, 0, complete=False)
    cumulative = _ranked_page_candidate(store_id, 1, values=range(2400, 2407))

    response = client.post(
        "/api/collection/browser-extension/store",
        json=_ranked_page_payload(store_id, [incomplete, cumulative]),
    )

    assert response.status_code == 201, response.text
    assert response.json()["added_count"] == 0
    assert response.json()["skipped_count"] == 2
    assert db_session.query(Product).filter_by(store_id=store["id"], status="active").count() == 0
    assert db_session.query(StoreSnapshot).filter_by(store_id=store["id"]).count() == 1
    state = db_session.get(StorePageDiscoveryState, store["id"])
    assert "page=2" in state.next_page_url


def test_invalid_or_stale_page_does_not_mutate_pool_or_cursor(client, db_session):
    store_id = "770735"
    store = client.post(
        "/api/stores",
        json={"name": "Stale Page Store", "url": f"https://www.aliexpress.com/store/{store_id}"},
    ).json()
    page_one = _ranked_page_payload(store_id, [])
    assert client.post("/api/collection/browser-extension/store", json=page_one).status_code == 201
    state = db_session.get(StorePageDiscoveryState, store["id"])
    cursor_before = state.next_page_url

    stale_payload = _ranked_page_payload(
        store_id, [_ranked_page_candidate(store_id, 1)], page=3
    )
    response = client.post("/api/collection/browser-extension/store", json=stale_payload)

    assert response.status_code == 409
    assert db_session.query(Product).filter_by(store_id=store["id"]).count() == 0
    assert state.next_page_url == cursor_before
