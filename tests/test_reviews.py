from app.db.models import Product, ProductReview, Store


def test_review_task_claim_scopes_postgres_row_lock_to_task_table():
    from sqlalchemy.dialects import postgresql

    from app.services.reviews import claim_next_task

    class CapturingSession:
        def __init__(self):
            self.claim_statement = None

        def scalars(self, _statement):
            return []

        def scalar(self, statement):
            self.claim_statement = statement
            return None

        def flush(self):
            pass

        def commit(self):
            pass

    session = CapturingSession()
    claim_next_task(session)
    sql = str(session.claim_statement.compile(dialect=postgresql.dialect()))

    assert "FOR UPDATE OF review_collection_tasks SKIP LOCKED" in sql


def test_review_task_reuse_claim_and_incremental_result(client, db_session):
    store = Store(name="Review Store", url="https://www.aliexpress.com/store/700001")
    product = Product(
        store=store,
        aliexpress_product_id="100500700001",
        url="https://www.aliexpress.com/item/100500700001.html",
        title="Wireless desk lamp",
    )
    db_session.add(product)
    db_session.commit()

    first = client.post(f"/api/products/{product.id}/reviews/collection-tasks", json={})
    second = client.post(f"/api/products/{product.id}/reviews/collection-tasks", json={})
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["id"] == second.json()["id"]

    claim = client.post("/api/reviews/collection-tasks/claim")
    assert claim.status_code == 200
    assert claim.json()["task"]["id"] == first.json()["id"]
    task_id = claim.json()["task"]["id"]

    result = {
        "status": "SUCCEEDED",
        "scanned_count": 2,
        "reviews": [
            {
                "review_text": "The lamp is bright and easy to install.",
                "rating": 5,
                "review_date": "2026-09-22",
                "country_region": "United States",
                "variant": "Warm white",
                "has_media": False,
                "verified_purchase": None,
                "raw_data": {"extractor_version": "fixture"},
            },
            {
                "review_text": "The lamp is bright and easy to install.",
                "rating": 5,
                "review_date": "2026-09-22",
                "country_region": "United States",
                "variant": "Warm white",
                "has_media": False,
                "verified_purchase": None,
                "raw_data": {"extractor_version": "fixture"},
            },
        ],
    }
    completed = client.post(f"/api/reviews/collection-tasks/{task_id}/result", json=result)
    assert completed.status_code == 200
    assert completed.json()["saved_count"] == 1
    assert db_session.query(ProductReview).count() == 1

    repeated = client.post(f"/api/products/{product.id}/reviews/collection-tasks", json={})
    assert repeated.status_code == 202
    assert repeated.json()["id"] != task_id
    second_claim = client.post("/api/reviews/collection-tasks/claim")
    assert second_claim.json()["task"]["id"] == repeated.json()["id"]
    duplicate_only = client.post(
        f"/api/reviews/collection-tasks/{repeated.json()['id']}/result",
        json={"status": "SUCCEEDED", "scanned_count": 1, "reviews": result["reviews"][:1]},
    )
    assert duplicate_only.json()["saved_count"] == 0
    assert db_session.query(ProductReview).count() == 1


def test_review_insights_role_filter_and_exports(client, db_session):
    self_store = Store(name="My Store", url="https://www.aliexpress.com/store/700002")
    peer_store = Store(name="Peer Store", url="https://www.aliexpress.com/store/700003")
    self_product = Product(
        store=self_store,
        aliexpress_product_id="100500700002",
        url="https://www.aliexpress.com/item/100500700002.html",
        title="Portable wireless lamp",
        business_role="SELF",
    )
    peer_product = Product(
        store=peer_store,
        aliexpress_product_id="100500700003",
        url="https://www.aliexpress.com/item/100500700003.html",
        title="Wireless charging lamp",
        business_role="COMPETITOR",
    )
    self_product.reviews.append(
        ProductReview(identity_hash="1" * 64, review_text="Easy bright wireless lamp", rating=5)
    )
    peer_product.reviews.append(
        ProductReview(identity_hash="2" * 64, review_text="Poor battery life", rating=2)
    )
    db_session.add_all([self_product, peer_product])
    db_session.commit()

    response = client.get("/api/reviews/insights?business_role=COMPETITOR")
    assert response.status_code == 200
    body = response.json()
    assert body["total_reviews"] == 1
    assert body["rating_distribution"]["2"] == 1
    assert all("portable" not in row["term"] for row in body["title_terms"])

    csv_export = client.get("/api/reviews/export?format=csv&business_role=SELF")
    assert csv_export.status_code == 200
    assert "Portable wireless lamp" in csv_export.text
    markdown = client.get("/api/reviews/export?format=markdown&business_role=SELF")
    assert "不代表平台搜索量" in markdown.text
    assert "给 ChatGPT 的中文分析提示词" in markdown.text


def test_product_role_update_validates_enum(client, db_session):
    store = Store(name="Role Store", url="https://www.aliexpress.com/store/700004")
    product = Product(
        store=store,
        aliexpress_product_id="100500700004",
        url="https://www.aliexpress.com/item/100500700004.html",
    )
    db_session.add(product)
    db_session.commit()

    updated = client.patch(
        f"/api/products/{product.id}", json={"business_role": "COMPETITOR"}
    )
    assert updated.status_code == 200
    assert updated.json()["business_role"] == "COMPETITOR"
    invalid = client.patch(f"/api/products/{product.id}", json={"business_role": "BUYER"})
    assert invalid.status_code == 422


def test_verification_task_can_resume_after_human_clearance(client, db_session):
    store = Store(name="Challenge Store", url="https://www.aliexpress.com/store/700005")
    product = Product(
        store=store,
        aliexpress_product_id="100500700005",
        url="https://www.aliexpress.com/item/100500700005.html",
    )
    db_session.add(product)
    db_session.commit()
    task = client.post(f"/api/products/{product.id}/reviews/collection-tasks", json={}).json()
    claim = client.post("/api/reviews/collection-tasks/claim").json()["task"]
    challenge = client.post(
        f"/api/reviews/collection-tasks/{task['id']}/challenge",
        json={"error_message": "需要人工验证"},
    )
    assert challenge.json()["status"] == "NEEDS_VERIFICATION"
    resumed = client.post(f"/api/reviews/collection-tasks/{task['id']}/resume", json={})
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "RUNNING"


def test_review_pages_render_empty_and_collected_states(client, db_session):
    store = Store(name="Page Store", url="https://www.aliexpress.com/store/700006")
    product = Product(
        store=store,
        aliexpress_product_id="100500700006",
        url="https://www.aliexpress.com/item/100500700006.html",
        title="Review page fixture",
        business_role="COMPETITOR",
    )
    db_session.add(product)
    db_session.commit()

    detail = client.get(f"/dashboard/products/{product.id}")
    insights = client.get("/reviews/insights?business_role=COMPETITOR")
    assert detail.status_code == 200
    assert "采集公开评论" in detail.text
    assert insights.status_code == 200
    assert "监控商品标题高频词" in insights.text
    assert "不代表 AliExpress 搜索量" in insights.text
