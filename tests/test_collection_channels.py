from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.collectors.base import CollectorResult
from app.collectors.dependencies import get_collector
from app.db.models import CollectionAttempt, ManualCollectionTask, Product, ProductSnapshot, StoreSnapshot


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


def test_ranked_store_enrollment_requires_twenty_when_store_has_at_least_twenty_known_products(client, db_session):
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
        "url": "https://www.aliexpress.com/store/770708?shop_sortType=orders_desc",
        "products": [
            {
                "platform_product_id": item.aliexpress_product_id,
                "url": item.url,
            }
            for item in products[:19]
        ],
        "raw_data": {
            "collection_mode": "top_7d_increment_selection",
            "unique_candidate_count": 21,
        },
    }

    response = client.post("/api/collection/browser-extension/store", json=payload)

    assert response.status_code == 409
    assert "20" in response.json()["detail"]
    assert all(item.status == "active" for item in products)


def test_ranked_store_enrollment_rejects_incomplete_ixspy_histories_even_when_flagged_complete(client, db_session):
    store = client.post(
        "/api/stores",
        json={"name": "Incomplete Ranked Store", "url": "https://www.aliexpress.com/store/770712"},
    ).json()
    products = []
    expected_dates = [
        "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25",
        "2026-09-26", "2026-09-27", "2026-09-28",
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
                "2026-09-22", "2026-09-23", "2026-09-25", "2026-09-26",
                "2026-09-27", "2026-09-28", "2026-09-29",
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
            "url": "https://www.aliexpress.com/store/770712?shop_sortType=orders_desc&page=2",
            "products": selected,
            "raw_data": {
                "collection_mode": "top_7d_increment_selection",
                "selection_method": "ixspy_visible_latest_7_day_increment_sum",
                "unique_candidate_count": 20,
            },
        },
    )

    assert response.status_code == 409
    assert "完整" in response.json()["detail"]
    assert all(item.status == "active" for item in products)
    assert db_session.query(ProductSnapshot).filter(
        ProductSnapshot.product_id.in_([item.id for item in products])
    ).count() == 0
    assert db_session.query(StoreSnapshot).filter_by(store_id=store["id"]).count() == 0


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
        "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25",
        "2026-09-26", "2026-09-27", "2026-09-28",
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
            "url": "https://www.aliexpress.com/store/770710?shop_sortType=orders_desc",
            "products": candidates,
            "raw_data": {
                "collection_mode": "top_7d_increment_selection",
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


def test_ranked_store_enrollment_reactivates_exact_top_twenty_from_fifty_one_inactive_products(client, db_session):
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
        "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25",
        "2026-09-26", "2026-09-27", "2026-09-28",
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
            "url": "https://www.aliexpress.com/store/770720?shop_sortType=orders_desc&page=2",
            "products": selection,
            "raw_data": {
                "collection_mode": "top_7d_increment_selection",
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
    assert response.json()["existing_count"] == 20
    assert response.json()["deactivated_count"] == 0
    active_products = list(
        db_session.query(Product)
        .filter_by(store_id=store["id"], status="active")
        .order_by(Product.discovery_rank)
    )
    assert [item.aliexpress_product_id for item in active_products] == [
        products_by_rank[index].aliexpress_product_id for index in selected_ranks
    ]
    assert db_session.query(Product).filter_by(store_id=store["id"], status="inactive").count() == 31
    assert db_session.query(ProductSnapshot).filter(
        ProductSnapshot.product_id.in_([item.id for item in active_products])
    ).count() == 20


def test_ranked_store_enrollment_deactivates_unselected_without_deleting_history(client, db_session):
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
            "history_expected_dates": ["2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-26", "2026-09-27", "2026-09-28"],
            "historical_sales": [
                    {"date": day, "value": index, "value_type": "daily_increment"}
                for day, index in zip(
                    ["2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-26", "2026-09-27", "2026-09-28"],
                    range(7),
                )
            ],
        }
        for item in products[:20]
    ]
    payload = {
        "platform_store_id": "770709",
        "url": "https://www.aliexpress.com/store/770709?shop_sortType=orders_desc&page=2",
        "products": selection,
        "raw_data": {
            "collection_mode": "top_7d_increment_selection",
            "unique_candidate_count": 21,
            "selection": [
                {"product_id": item["platform_product_id"], "selection_rank": index + 1, "selection_reason": "latest_7d_increment"}
                for index, item in enumerate(selection)
            ],
        },
    }

    response = client.post("/api/collection/browser-extension/store", json=payload)

    assert response.status_code == 201, response.text
    assert response.json()["deactivated_count"] == 1
    assert all(item.status == "active" for item in products[:20])
    assert products[-1].status == "inactive"
    assert db_session.query(ProductSnapshot).filter_by(product_id=products[-1].id).count() == 1
    store_snapshot = db_session.query(StoreSnapshot).filter_by(store_id=store["id"]).one()
    assert store_snapshot.raw_payload["raw_data"]["selection"][0]["selection_rank"] == 1


def test_ranked_store_enrollment_replaces_manual_and_store_page_products_exactly(client, db_session):
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
        "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25",
        "2026-09-26", "2026-09-27", "2026-09-28",
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
            "url": "https://www.aliexpress.com/store/770721?shop_sortType=orders_desc",
            "products": selected,
            "raw_data": {
                "collection_mode": "top_7d_increment_selection",
                "unique_candidate_count": 22,
            },
        },
    )

    assert response.status_code == 201, response.text
    active_products = db_session.query(Product).filter_by(store_id=store["id"], status="active").all()
    assert {item.aliexpress_product_id for item in active_products} == {
        item["platform_product_id"] for item in selected
    }
    assert len(active_products) == 20
    assert products[19].status == "active"
    assert products[20].status == "inactive"
    assert products[21].status == "inactive"


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
        "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25",
        "2026-09-26", "2026-09-27", "2026-09-28",
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
            "url": "https://www.aliexpress.com/store/770717?shop_sortType=orders_desc&page=2",
            "products": candidates,
            "raw_data": {
                "collection_mode": "top_7d_increment_selection",
                "selection_method": "ixspy_visible_latest_7_day_increment_sum",
                "unique_candidate_count": 20,
            },
        },
    )

    assert response.status_code == 409
    assert "未修改监控池" in response.json()["detail"]
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
            "url": "https://www.aliexpress.com/store/770725?shop_sortType=orders_desc",
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
                "unique_candidate_count": 1,
            },
        },
    )

    assert response.status_code == 409
    assert db_session.query(Product).filter_by(store_id=store["id"]).count() == 0
    assert db_session.query(StoreSnapshot).filter_by(store_id=store["id"]).count() == 0


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
