from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.db.models import Product, ProductDailyMetric, ProductSnapshot, Store


def test_dashboard_renders_empty_state(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "竞品监控台" in response.text
    assert "还没有活跃店铺" in response.text
    assert "非真实后台订单量" not in response.text
    assert '<details id="dianxiaomi-status"' in response.text
    assert '<summary class="section-heading dianxiaomi-summary">' in response.text
    assert '<details id="dianxiaomi-status" class="data-section dianxiaomi-band" data-status-endpoint="/api/integrations/dianxiaomi/status" open' not in response.text


def test_dianxiaomi_queue_uses_product_id_instead_of_product_title(client):
    script = client.get("/static/dashboard.js")

    assert script.status_code == 200
    assert "商品 ID ${item.platform_product_id || item.product_id" in script.text
    assert 'item.product_title || item.platform_product_id' not in script.text


def test_dashboard_renders_product_and_failed_snapshot(client, db_session):
    store = Store(name="Visible Store", url="https://www.aliexpress.com/store/660001")
    product = Product(
        store=store,
        aliexpress_product_id="100500660",
        url="https://www.aliexpress.com/item/100500660.html",
        title="Visible Product",
    )
    snapshot = ProductSnapshot(
        product=product,
        collected_at=datetime(2026, 9, 20, 2, tzinfo=timezone.utc),
        collector_name="fixture",
        collector_version="1",
        parse_status="failed",
        raw_payload={"kept": True},
        error_type="page_structure_unrecognized",
    )
    db_session.add(snapshot)
    db_session.commit()

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "Visible Product" in dashboard.text
    assert "failed" in dashboard.text

    detail = client.get(f"/dashboard/products/{product.id}")
    assert detail.status_code == 200
    assert "只追加，不覆盖" in detail.text
    assert "page_structure_unrecognized" in detail.text


def test_dashboard_expands_store_with_product_and_seven_day_sales(client, db_session):
    store = Store(name="Store A", url="https://www.aliexpress.com/store/660002")
    product = Product(
        store=store,
        aliexpress_product_id="100500661",
        url="https://www.aliexpress.com/item/100500661.html",
        title="Tracked Product",
    )
    yesterday = datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)
    db_session.add_all(
        [
            ProductDailyMetric(
                product=product,
                metric_date=yesterday,
                estimated_sales=7,
                is_estimable=True,
                reason="estimated_from_public_cumulative_delta",
            ),
            ProductSnapshot(
                product=product,
                collected_at=datetime.now(timezone.utc),
                collector_name="fixture",
                collector_version="1",
                parse_status="success",
                cumulative_sold=107,
                raw_payload={},
            ),
        ]
    )
    db_session.commit()

    response = client.get("/")

    assert response.status_code == 200
    assert 'href="#active-stores"' in response.text
    assert "Store A" in response.text
    assert "Tracked Product" in response.text
    assert "最近估算日销" in response.text
    assert "历史 7 天日销" in response.text
    assert ">7<" in response.text


def test_dashboard_marks_discovered_product_pending_and_links_to_product(client, db_session):
    store = Store(name="Discovered Store", url="https://www.aliexpress.com/store/660004")
    product = Product(
        store=store,
        aliexpress_product_id="100500663",
        url="https://www.aliexpress.com/item/100500663.html",
        discovery_source="chrome_extension_store",
    )
    db_session.add(product)
    db_session.commit()

    response = client.get(f"/dashboard/stores/{store.id}")

    assert response.status_code == 200
    assert "待采集" in response.text
    assert f'href="{product.url}#monitor_product_id={product.aliexpress_product_id}"' in response.text
    assert f'data-product-id="{product.id}"' in response.text
    assert "删除监控" in response.text


def test_dashboard_has_product_deactivate_control(client, db_session):
    store = Store(name="Remove Store", url="https://www.aliexpress.com/store/660005")
    product = Product(
        store=store,
        aliexpress_product_id="100500664",
        url="https://www.aliexpress.com/item/100500664.html",
    )
    db_session.add(product)
    db_session.commit()

    response = client.get(f"/dashboard/stores/{store.id}")

    assert response.status_code == 200
    assert f'data-product-id="{product.id}"' in response.text
    assert "删除监控" in response.text


def test_dashboard_keeps_rendering_after_product_is_deactivated(client, db_session):
    store = Store(name="Inactive Store", url="https://www.aliexpress.com/store/660006")
    product = Product(
        store=store,
        aliexpress_product_id="100500665",
        url="https://www.aliexpress.com/item/100500665.html",
        status="inactive",
    )
    db_session.add(
        ProductSnapshot(
            product=product,
            collected_at=datetime.now(timezone.utc),
            collector_name="fixture",
            collector_version="1",
            parse_status="success",
            cumulative_sold=12,
            raw_payload={},
        )
    )
    db_session.commit()

    response = client.get("/")

    assert response.status_code == 200
    assert "Inactive Store" in response.text


def test_dashboard_labels_saved_challenge_page_clearly(client, db_session):
    store = Store(name="Challenge Store", url="https://www.aliexpress.com/store/660003")
    product = Product(
        store=store,
        aliexpress_product_id="100500662",
        url="https://www.aliexpress.com/item/100500662.html",
    )
    db_session.add(
        ProductSnapshot(
            product=product,
            collected_at=datetime.now(timezone.utc),
            collector_name="fixture",
            collector_version="1",
            parse_status="failed",
            raw_payload={},
            raw_content='<script>{"action":"captcha","url":"/punish?x=1"}</script>',
            error_type="page_structure_unrecognized",
        )
    )
    db_session.commit()

    response = client.get("/")

    assert response.status_code == 200
    assert "平台验证页" in response.text
    assert ">failed<" not in response.text


def test_dashboard_css_and_store_detail_are_available(client, db_session):
    store = Store(name="Detail Store", url="https://www.aliexpress.com/store/770001")
    db_session.add(store)
    db_session.commit()
    assert client.get("/static/dashboard.css").status_code == 200
    detail = client.get(f"/dashboard/stores/{store.id}")
    assert detail.status_code == 200
    assert "仅覆盖" in detail.text
    assert "已监控商品求和" in detail.text
    assert 'class="store-rename-form inline-rename"' in detail.text
    assert "编辑店铺名称" in detail.text


def test_manual_processing_page_has_stable_alias(client):
    pending = client.get("/collection/pending")
    manual = client.get("/manual")

    assert pending.status_code == 200
    assert manual.status_code == 200
    assert "待人工补采" in manual.text


def test_dashboard_exposes_store_rename_control(client, db_session):
    store = Store(name="Original Name", url="https://www.aliexpress.com/store/770002")
    db_session.add(store)
    db_session.commit()

    dashboard = client.get("/")

    assert dashboard.status_code == 200
    assert f'data-store-id="{store.id}"' in dashboard.text
    assert 'value="Original Name"' in dashboard.text
    assert "编辑店铺名称" in dashboard.text
    assert "采集到店小秘" in dashboard.text
