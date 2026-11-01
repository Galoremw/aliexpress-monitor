from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.db.models import (
    BrowserCollectionControl,
    BrowserCollectionItem,
    BrowserCollectionRun,
    BrowserCollectionWorker,
    Product,
    ProductDailyMetric,
    ProductSnapshot,
    Store,
)
from app.services.metrics import calculate_product_daily_metrics


def test_dashboard_renders_empty_state(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "竞品监控台" in response.text
    assert "还没有活跃店铺" in response.text
    assert 'id="browser-worker-state"' in response.text
    assert "扩展在线但队列超过 2 分钟未推进" in client.get("/static/dashboard.js").text
    assert 'id="browser-run-now"' in response.text
    assert 'id="browser-run-pause"' in response.text
    assert 'id="browser-run-resume"' not in response.text
    assert "非真实后台订单量" not in response.text
    assert '<details id="dianxiaomi-status"' in response.text
    assert '<summary class="section-heading dianxiaomi-summary">' in response.text
    assert '<details id="dianxiaomi-status" class="data-section dianxiaomi-band" data-status-endpoint="/api/integrations/dianxiaomi/status" open' not in response.text
    assert 'dashboard.js?v=shared-workers-20261031' in response.text
    assert 'id="browser-run-cancel"' in response.text
    script = client.get("/static/dashboard.js").text
    collect_all_handler = script.split('document.querySelector("#collect-all")', 1)[1].split(
        'document.querySelectorAll(".collect-one")', 1
    )[0]
    assert '"/api/browser-collection/runs/collect-all"' in collect_all_handler
    assert '"/api/jobs/collect-now"' not in collect_all_handler


def test_dianxiaomi_panel_shows_shared_collector_without_second_selector():
    dashboard = Path("app/templates/dashboard.html").read_text(encoding="utf-8")
    script = Path("app/static/dashboard.js").read_text(encoding="utf-8")

    assert 'id="dianxiaomi-worker-online"' in dashboard
    assert 'id="dianxiaomi-owner-waiting"' in dashboard
    assert 'id="browser-collector-select"' in dashboard
    assert 'id="dianxiaomi-collector-select"' not in dashboard
    assert "当前采集电脑" in dashboard
    assert "status.selected_worker" in script
    assert "status.waiting_reason" in script


def test_galore_collector_launch_button_is_local_only(client):
    local_dashboard = client.get("/", headers={"host": "127.0.0.1:3000"})
    lan_dashboard = client.get("/", headers={"host": "192.168.66.115:3000"})

    assert local_dashboard.status_code == 200
    assert 'id="launch-galore-collector"' in local_dashboard.text
    assert 'href="aliexpress-monitor-collector://start"' in local_dashboard.text
    assert "Galore主机采集" in local_dashboard.text
    assert 'id="launch-galore-collector"' not in lan_dashboard.text


def test_dashboard_renders_worker_selector_and_separate_start_button(client):
    dashboard = client.get("/").text
    script = client.get("/static/dashboard.js").text

    assert 'id="browser-collector-select"' in dashboard
    assert 'id="browser-selected-worker-state"' in dashboard
    assert 'id="browser-run-start"' in dashboard
    assert 'id="browser-run-now"' in dashboard
    assert "重新检查今日任务" in dashboard
    assert '"/api/browser-collection/runs/start-today"' in script


def test_dashboard_renders_helper_selector_and_locked_active_roles(client, db_session):
    store = client.post(
        "/api/stores",
        json={"name": "Role UI Store", "url": "https://www.aliexpress.com/store/991001"},
    ).json()
    client.post(
        "/api/products",
        json={"store_id": store["id"], "aliexpress_product_id": "1005009910011"},
    )
    for worker_id, label in (("ui-primary", "QX电脑"), ("ui-helper", "Galore主机")):
        assert client.post(
            "/api/browser-collection/heartbeat",
            json={"worker_id": worker_id, "worker_label": label},
        ).status_code == 200
    helper = db_session.get(BrowserCollectionWorker, "ui-helper")
    helper.last_heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=3)
    db_session.commit()
    assert client.post(
        "/api/browser-collection/control/worker", json={"worker_id": "ui-primary"}
    ).status_code == 200
    started = client.post(
        "/api/browser-collection/runs/start-today",
        json={"helper_worker_ids": ["ui-helper"]},
    )
    assert started.status_code == 200, started.text

    dashboard = client.get("/").text
    helper_options = dashboard.split('id="browser-helper-collectors-select"', 1)[1].split(
        "</select>", 1
    )[0]

    assert 'id="browser-helper-collectors-select" multiple' in dashboard
    assert "辅助采集电脑" in dashboard
    assert "ui-primary" not in helper_options
    assert "ui-helper" in helper_options
    assert "· 离线" in helper_options
    assert 'id="browser-run-locked-roles"' in dashboard
    assert 'data-primary-worker-id="ui-primary"' in dashboard
    assert "ui-primary" in dashboard.split('id="browser-run-locked-roles"', 1)[1].split("</div>", 1)[0]
    assert "ui-helper" in dashboard.split('id="browser-run-locked-roles"', 1)[1].split("</div>", 1)[0]


def test_dashboard_sends_helper_ids_for_first_start_and_legacy_resume(client):
    script = client.get("/static/dashboard.js").text
    start_handler = script.split('document.querySelector("#browser-run-start")?.addEventListener', 1)[1].split(
        'document.querySelector("#browser-run-now")', 1
    )[0]

    assert 'document.querySelector("#browser-helper-collectors-select")' in start_handler
    assert "helper_worker_ids" in start_handler
    assert "browserCollectionRun?.primary_worker_id" in start_handler
    assert 'jsonRequest(`/api/browser-collection/runs/${browserCollectionRun.id}/resume`' in start_handler
    assert 'jsonRequest("/api/browser-collection/runs/start-today"' in start_handler


def test_dashboard_enables_resume_when_authorized_helper_is_online(client, db_session):
    now = datetime.now(timezone.utc)
    primary = BrowserCollectionWorker(
        worker_id="dashboard-offline-primary", label="Primary",
        last_heartbeat_at=now - timedelta(minutes=3),
    )
    helper = BrowserCollectionWorker(
        worker_id="dashboard-online-helper", label="Helper", last_heartbeat_at=now,
    )
    run = BrowserCollectionRun(
        target_date=datetime.now().date(), status="PAUSED", phase="STORE_DISCOVERY",
        total_count=1, primary_worker_id=primary.worker_id, helper_workers=[helper],
    )
    item = BrowserCollectionItem(
        run=run, target_type="STORE", target_key="store:ui-resume",
        target_url="https://www.aliexpress.com/store/ui-resume/pages/all-items.html",
        position=1, status="PENDING",
    )
    control = BrowserCollectionControl(
        id=1, selected_worker_id=primary.worker_id, selected_worker_label=primary.label
    )
    db_session.add_all([primary, helper, run, item, control])
    db_session.commit()

    dashboard = client.get("/").text

    assert 'id="browser-run-start" class="button primary" type="button">继续采集</button>' in dashboard


def test_dashboard_disables_start_for_offline_worker(client, db_session):
    client.post(
        "/api/browser-collection/heartbeat",
        json={"worker_id": "offline-worker", "worker_label": "离线电脑"},
    )
    control = db_session.get(BrowserCollectionControl, 1)
    if control is None:
        control = BrowserCollectionControl(id=1)
        db_session.add(control)
        db_session.flush()
    control.selected_worker_id = "offline-worker"
    control.selected_worker_label = "离线电脑"
    worker = db_session.get(BrowserCollectionWorker, "offline-worker")
    worker.last_heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=3)
    db_session.commit()

    dashboard = client.get("/").text

    assert 'id="browser-run-start" class="button primary" type="button" disabled' in dashboard
    assert "离线电脑 · 离线" in dashboard


def test_worker_selection_and_refresh_do_not_start_queue(client):
    script = client.get("/static/dashboard.js").text
    selection_handler = script.split('document.querySelector("#browser-collector-select")?.addEventListener', 1)[1].split(
        'document.querySelector("#browser-run-start")', 1
    )[0]
    refresh_handler = script.split("async function refreshBrowserCollectionControl()", 1)[1].split(
        'document.querySelector("#browser-collector-select")', 1
    )[0]

    assert '"/api/browser-collection/control/worker"' in selection_handler
    assert "/api/browser-collection/runs/start-today" not in selection_handler
    assert "aliexpress-monitor:browser-run-requested" not in selection_handler
    assert 'jsonRequest("/api/browser-collection/control")' in refresh_handler
    assert 'method: "POST"' not in refresh_handler


def test_dashboard_worker_switch_does_not_prompt_for_handoff(client):
    dashboard = client.get("/").text
    script = client.get("/static/dashboard.js").text
    selection_handler = script.split(
        'document.querySelector("#browser-collector-select")?.addEventListener', 1
    )[1].split('document.querySelector("#browser-run-start")', 1)[0]

    assert "只有这些电脑可以领取新任务" in dashboard
    assert "等待旧采集任务结束" not in dashboard
    assert "电脑切换需要等待旧任务结束" not in dashboard
    assert "window.confirm" not in selection_handler
    assert "confirm_handoff" not in selection_handler
    assert 'dashboard.css?v=primary-helpers-20261101' in dashboard
    assert 'dashboard.js?v=primary-helpers-20261101' in dashboard


def test_dashboard_shows_shared_claim_counts(client):
    stores = [
        client.post(
            "/api/stores",
            json={"name": "Shared Worker Store", "url": f"https://www.aliexpress.com/store/{store_id}"},
        ).json()
        for store_id in ("990374", "990375")
    ]
    for index, store in enumerate(stores, start=1):
        client.post(
            "/api/products",
            json={"store_id": store["id"], "aliexpress_product_id": f"10050099374{index}"},
        )
    for worker_id in ("worker-alpha-1234", "worker-beta-5678"):
        response = client.post(
            "/api/browser-collection/heartbeat",
            json={"worker_id": worker_id, "worker_label": "采集电脑", "extension_version": "test"},
        )
        assert response.status_code == 200
    assert client.post(
        "/api/browser-collection/control/worker", json={"worker_id": "worker-alpha-1234"}
    ).status_code == 200
    assert client.post(
        "/api/browser-collection/runs/start-today",
        json={"helper_worker_ids": ["worker-beta-5678"]},
    ).status_code == 200
    first = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-alpha-1234"}
    ).json()
    second = client.post(
        "/api/browser-collection/items/claim", json={"worker_id": "worker-beta-5678"}
    ).json()
    assert first["item"] is not None
    assert second["item"] is not None

    dashboard = client.get("/").text
    script = client.get("/static/dashboard.js").text

    assert 'id="browser-worker-loads"' in dashboard
    assert "采集电脑 · worker-a" in dashboard
    assert "采集电脑 · worker-b" in dashboard
    assert 'data-worker-claim-count="1"' in dashboard
    assert 'worker.display_label' in script
    assert 'worker.active_claim_count' in script
    assert "正在处理" in dashboard


def test_queue_preparation_does_not_dispatch_or_wake_worker(client):
    script = client.get("/static/dashboard.js").text
    collect_all_handler = script.split('document.querySelector("#collect-all")', 1)[1].split(
        'document.querySelectorAll(".collect-one")', 1
    )[0]
    recheck_handler = script.split('document.querySelector("#browser-run-now")', 1)[1].split(
        'document.querySelector("#browser-collection-status")', 1
    )[0]
    retry_handler = script.split('const action = event.target.closest("[data-browser-item-action]")', 1)[1].split(
        'document.querySelector("#browser-run-pause")', 1
    )[0]
    store_handler = script.split('document.querySelectorAll(".store-collect-products")', 1)[1].split(
        'document.querySelector("#product-form")', 1
    )[0]

    assert "aliexpress-monitor:browser-run-requested" not in collect_all_handler
    assert "aliexpress-monitor:browser-run-requested" not in recheck_handler
    assert "aliexpress-monitor:browser-run-requested" not in retry_handler
    assert "aliexpress-monitor:browser-run-requested" not in store_handler


def test_dashboard_section_navigation_links_resolve_to_sections(client):
    dashboard = client.get("/").text
    targets = (
        "browser-collection-status",
        "active-stores",
        "add-store",
    )

    assert 'id="dashboard-section-nav"' in dashboard
    for target in targets:
        assert f'href="#{target}"' in dashboard
        assert f'id="{target}"' in dashboard
    assert "每日浏览器采集" in dashboard
    assert "监控店铺" in dashboard
    assert "添加监控店铺" in dashboard
    assert 'aria-label="监控台区块导航"' in dashboard


def test_store_add_form_is_above_collection_progress(client):
    response = client.get("/")

    assert response.status_code == 200
    workspace = response.text.split('<section class="workspace" id="add-monitor">', 1)[1].split("</section>", 1)[0]
    assert workspace.index('id="store-form"') < workspace.index('id="product-form"')
    assert response.text.index('<section class="workspace" id="add-monitor">') < response.text.index('id="collection-progress"')


def test_dianxiaomi_queue_uses_product_id_instead_of_product_title(client):
    dashboard = client.get("/")
    script = client.get("/static/dashboard.js")

    assert dashboard.status_code == 200
    assert 'role="tablist"' in dashboard.text
    assert 'id="open-dedicated-chrome"' not in dashboard.text
    assert "任务会先排队并保持待启动" in dashboard.text
    assert "position: sticky" in client.get("/static/dashboard.css").text
    assert 'data-dianxiaomi-filter="queued"' in dashboard.text
    assert 'data-dianxiaomi-filter="failed"' in dashboard.text
    assert 'data-dianxiaomi-filter="succeeded"' in dashboard.text
    assert 'data-dianxiaomi-filter="queued"><small>待发送</small>' in dashboard.text
    assert "列表显示最近 100 项" in dashboard.text
    assert "监控商品库" in dashboard.text
    assert "不属于店小秘队列" in dashboard.text
    assert '<details class="store-monitor" open>' not in dashboard.text
    assert '<details class="data-section recent-snapshots" id="recent-snapshots">' in dashboard.text
    assert '<details class="data-section recent-snapshots" id="recent-snapshots" open>' not in dashboard.text
    assert script.status_code == 200
    assert "updateBrowserCollection(result.run)" in script.text
    assert "aliexpress-monitor:browser-run-requested" in script.text
    assert 'document.querySelector("#browser-run-start")' in script.text
    assert '"/api/browser-collection/runs/start-today"' in script.text
    assert '`/api/browser-collection/runs/${browserCollectionRun.id}/resume`' in script.text
    assert '`/api/browser-collection/runs/${button.dataset.runId}/cancel`' in script.text
    assert "取消剩余采集队列" in script.text
    assert "正在采集的店铺任务可能完成" in script.text
    assert 'item.platform_product_id || item.product_id ||' in script.text
    assert 'item.product_title || item.platform_product_id' not in script.text
    assert 'failed: ["FAILED"]' in script.text
    assert 'queued: ["QUEUED"]' in script.text
    assert 'let dianxiaomiActiveFilter = "queued"' in script.text


def test_store_collection_prepares_queue_without_waking_extension(client):
    script = client.get("/static/dashboard.js")
    handler = script.text.split('document.querySelectorAll(".store-collect-products")', 1)[1].split('document.querySelector("#product-form")', 1)[0]

    assert "updateBrowserCollection(result.run)" in handler
    assert 'window.dispatchEvent(new Event("aliexpress-monitor:browser-run-requested"))' not in handler
    assert "refreshBrowserCollectionControl()" in handler


def test_daily_queue_copy_promises_store_pages_not_product_fallback(client):
    dashboard = client.get("/").text
    script = client.get("/static/dashboard.js").text

    assert "店铺商品展示页读取已监控商品的 IXSPY 最近 7 日增量" in dashboard
    assert "缺失日期标记待补，下次店铺页任务重试" in script
    assert "缺失的七日日增量会由商品页补采" not in script
    assert "队列空闲" in dashboard
    assert "当前店铺任务完成后不会继续下一项" in script
    assert "可从监控店铺列表单独启动店铺页采集" in script
    assert "当前商品完成后不会继续下一项" not in script


def test_new_store_waits_for_ranked_ixspy_intake_in_both_dashboards(client):
    dashboard = client.get("/").text
    local_script = client.get("/static/dashboard.js").text
    local_add = local_script.split('document.querySelector("#store-form")', 1)[1].split(
        'document.querySelectorAll(".store-collect-products")', 1
    )[0]
    hosted = Path("frontend/src/app.js").read_text(encoding="utf-8")
    hosted_add = hosted.split("async function addStore()", 1)[1].split("async function addProduct()", 1)[0]

    assert "按近 7 日增量筛选并入池" in dashboard
    assert "/discover?limit=20" not in local_add
    assert "/browser-collection" not in local_add
    assert "/discover?limit=20" not in hosted_add
    assert 'data-action="discover"' not in hosted
    assert "data-action=\"collect-store\"" in hosted
    assert "/browser-collection`" in hosted


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


def test_dashboard_shows_zero_for_estimable_no_growth(client, db_session):
    store = Store(name="Zero Growth Store", url="https://www.aliexpress.com/store/660009")
    product = Product(
        store=store,
        aliexpress_product_id="100500669",
        url="https://www.aliexpress.com/item/100500669.html",
        title="Zero Growth Product",
    )
    db_session.add(product)
    db_session.flush()
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    yesterday = today - timedelta(days=1)
    db_session.add(
        ProductDailyMetric(
            product=product,
            metric_date=yesterday,
            estimated_sales=0,
            is_estimable=True,
            reason="ixspy_visible_daily_increment",
            calculation_method="ixspy_visible_daily_increment",
        )
    )
    db_session.commit()

    response = client.get("/")

    assert response.status_code == 200
    row = response.text.split(f'<tr data-product-id="{product.id}">', 1)[1].split("</tr>", 1)[0]
    assert f'<strong class="number">0</strong><small>{yesterday}</small>' in row


def test_dashboard_expands_store_with_product_and_seven_day_sales(client, db_session):
    store = Store(name="Store A", url="https://www.aliexpress.com/store/660002")
    product = Product(
        store=store,
        aliexpress_product_id="100500661",
        url="https://www.aliexpress.com/item/100500661.html",
        title="Tracked Product 11 sold $ 1.09 New shoppers save $0.50",
    )
    yesterday = datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)
    history_dates = [yesterday - timedelta(days=offset) for offset in range(6, -1, -1)]
    daily_metrics = [
        ProductDailyMetric(
            product=product,
            metric_date=metric_date,
            estimated_sales=11 if metric_date == yesterday else index,
            is_estimable=True,
            reason="ixspy_visible_daily_increment",
            calculation_method="ixspy_visible_daily_increment",
        )
        for index, metric_date in enumerate(history_dates, start=1)
    ]
    db_session.add_all(
        [
            *daily_metrics,
            ProductSnapshot(
                product=product,
                collected_at=datetime.now(timezone.utc),
                collector_name="fixture",
                collector_version="1",
                parse_status="success",
                cumulative_sold=107,
                raw_data={"historical_sales": [{"date": "2026-09-20", "value": 7}], "history_source": "visible_chart_tooltip"},
            ),
            ProductSnapshot(
                product=product,
                collected_at=datetime.now(timezone.utc) + timedelta(seconds=1),
                collector_name="store_listing",
                collector_version="1",
                parse_status="success",
                cumulative_sold=107,
                raw_data={"main_image_url": "https://ae01.alicdn.com/kf/product-main.jpg"},
            ),
            ProductSnapshot(
                product=product,
                collected_at=datetime.now(timezone.utc) + timedelta(seconds=2),
                collector_name="fixture_failure_context",
                collector_version="1",
                parse_status="failed",
                raw_data={"attempt": "no image on this response"},
            ),
        ]
    )
    db_session.commit()

    response = client.get("/")

    assert response.status_code == 200
    assert 'href="#active-stores"' in response.text
    assert "Store A" in response.text
    assert "Tracked Product" in response.text
    assert "11 sold" not in response.text
    assert "$ 1.09" not in response.text
    assert response.text.index("product-main.jpg") < response.text.index("Tracked Product")
    assert 'class="product-thumb"' in response.text
    assert "最近 IXSPY 日增量" in response.text
    assert "当前累计售出" not in response.text
    assert "历史 7 天日增量" in response.text
    assert "IXSPY 日增量 7 / 7 天" in response.text
    assert f'title="{yesterday} · IXSPY 页面可见日增量"' in response.text
    assert ">11<" in response.text
    assert "IXSPY 完整 7 天日增量合计" in response.text


def test_dashboard_explains_store_discovery_snapshot_has_no_ixspy_history(client, db_session):
    store = Store(name="Discovery Only Store", url="https://www.aliexpress.com/store/660008")
    product = Product(
        store=store,
        aliexpress_product_id="100500668",
        url="https://www.aliexpress.com/item/100500668.html",
        title="Store discovery only product",
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(
        ProductSnapshot(
            product=product,
            collected_at=datetime.now(timezone.utc),
            collector_name="chrome_extension_store",
            collector_version="1",
            parse_status="success",
            cumulative_sold=193,
            sold_count=193,
            raw_data={},
        )
    )
    db_session.commit()

    response = client.get("/")

    assert response.status_code == 200
    row = response.text.split(f'<tr data-product-id="{product.id}">', 1)[1].split("</tr>", 1)[0]
    assert "无直接数据" in row


def test_dashboard_keeps_ixspy_daily_increment_on_its_visible_date(client, db_session):
    store = Store(name="IXSPY Daily Store", url="https://www.aliexpress.com/store/660006")
    product = Product(
        store=store,
        aliexpress_product_id="100500665",
        url="https://www.aliexpress.com/item/100500665.html",
        title="Visible daily increment",
    )
    yesterday = datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)
    db_session.add(
        ProductDailyMetric(
            product=product,
            metric_date=yesterday,
            estimated_sales=13,
            is_estimable=True,
            reason="ixspy_visible_daily_increment",
            calculation_method="ixspy_visible_daily_increment",
        )
    )
    db_session.commit()

    response = client.get("/")

    assert response.status_code == 200
    assert ">13<" in response.text
    assert f"{yesterday}" in response.text
    assert "IXSPY 日增量 1 / 7 天" in response.text


def test_dashboard_uses_latest_cumulative_snapshot_consistent_with_visible_text(client, db_session):
    store = Store(name="Count Consistency Store", url="https://www.aliexpress.com/store/660008")
    product = Product(
        store=store,
        aliexpress_product_id="1005006680000001",
        url="https://www.aliexpress.com/item/1005006680000001.html",
        title="Count Consistency Product",
    )
    visible_text = "Count Consistency Product 3,000+ sold (3676 Orders)"
    db_session.add_all(
        [
            ProductSnapshot(
                product=product,
                collected_at=datetime(2026, 9, 28, 10),
                captured_at=datetime(2026, 9, 28, 10),
                collector_name="chrome_extension",
                collector_version="0.6.26",
                parse_status="success",
                source="CHROME_EXTENSION",
                status="VALID",
                cumulative_sold=3676,
                sold_count=3676,
                raw_data={"visible_text_excerpt": visible_text},
            ),
            ProductSnapshot(
                product=product,
                collected_at=datetime(2026, 9, 29, 10),
                captured_at=datetime(2026, 9, 29, 10),
                collector_name="chrome_extension",
                collector_version="0.6.26",
                parse_status="success",
                source="CHROME_EXTENSION",
                status="VALID",
                cumulative_sold=356,
                sold_count=356,
                raw_data={"visible_text_excerpt": visible_text},
            ),
        ]
    )
    db_session.commit()

    response = client.get("/")

    assert response.status_code == 200
    row = response.text.split(f'<tr data-product-id="{product.id}">', 1)[1].split("</tr>", 1)[0]
    assert '<strong class="number">3676</strong>' not in row
    assert '<strong class="number">356</strong>' not in row
    assert "无直接数据" in row


def test_growth_trends_are_compact_and_view_opens_product_snapshots(client, db_session):
    store = Store(name="Compact Trend Store", url="https://www.aliexpress.com/store/660007")
    product = Product(
        store=store,
        aliexpress_product_id="100500667",
        url="https://www.aliexpress.com/item/100500667.html",
        title="Compact Trend Product",
    )
    metric_date = datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)
    db_session.add_all(
        [
            ProductDailyMetric(
                product=product,
                metric_date=metric_date,
                estimated_sales=17,
                is_estimable=True,
                reason="ixspy_visible_daily_increment",
                calculation_method="ixspy_visible_daily_increment",
            ),
            ProductSnapshot(
                product=product,
                collector_name="fixture",
                collector_version="1",
                parse_status="success",
                source="CHROME_EXTENSION",
                status="VALID",
                cumulative_sold=42,
                raw_data={"main_image_url": "https://ae01.alicdn.com/kf/compact-product.jpg"},
            ),
        ]
    )
    db_session.commit()

    response = client.get("/")

    assert response.status_code == 200
    growth = response.text.split('<div class="growth-list">', 1)[1].split("</div>\n  </div>", 1)[0]
    assert "Compact Trend Store" in growth
    assert "compact-product.jpg" in growth
    assert ">+17<" in growth
    assert "IXSPY 页面可见日增量" in response.text
    assert f"{metric_date}" in growth
    assert f'href="/dashboard/products/{product.id}"' in growth
    assert ">查看</a>" in growth
    assert "<table" not in growth

    detail = client.get(f"/dashboard/products/{product.id}")
    assert detail.status_code == 200
    assert "原始快照历史" in detail.text
    assert "累计 sold" not in detail.text


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
    assert "product-thumb-empty" in response.text
    assert f'href="{product.url}#monitor_product_id={product.aliexpress_product_id}"' in response.text
    assert f'data-product-id="{product.id}"' in response.text


def test_product_detail_explains_ixspy_history_capture(client, db_session):
    store = Store(name="IXSPY Store", url="https://www.aliexpress.com/store/660005")
    product = Product(
        store=store,
        aliexpress_product_id="100500664",
        url="https://www.aliexpress.com/item/100500664.html",
        title="IXSPY History Product 3,000+ sold HK$ 35.57 New shoppers save HK$12.00",
    )
    db_session.add(product)
    db_session.flush()
    db_session.add(
        ProductSnapshot(
            product=product,
            collector_name="chrome_extension",
            collector_version="extension-content-0.6.1",
            parse_status="success",
            source="CHROME_EXTENSION",
            status="VALID",
            raw_data={
                "historical_sales": [
                    {"date": "2026-09-21", "value": 8, "value_type": "daily_increment"},
                    {"date": "2026-09-22", "value": 5, "value_type": "daily_increment"},
                ],
                "history_provider": "IXSPY_VISIBLE_UI",
            },
        )
    )
    db_session.add_all(
        [
            ProductDailyMetric(
                product=product,
                metric_date=datetime(2026, 9, 21).date(),
                estimated_sales=8,
                is_estimable=True,
                reason="ixspy_visible_daily_increment",
                calculation_method="ixspy_visible_daily_increment",
            ),
            ProductDailyMetric(
                product=product,
                metric_date=datetime(2026, 9, 22).date(),
                estimated_sales=5,
                is_estimable=True,
                reason="ixspy_visible_daily_increment",
                calculation_method="ixspy_visible_daily_increment",
            ),
        ]
    )
    db_session.commit()

    response = client.get(f"/dashboard/products/{product.id}")

    assert response.status_code == 200
    assert "IXSPY 日增量历史" in response.text
    assert "只显示图表中直接读取的数值" in response.text
    assert "IXSPY History Product" in response.text
    assert "3,000+ sold" not in response.text
    assert "HK$ 35.57" not in response.text
    assert ">8<" in response.text
    assert ">5<" in response.text
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


def test_store_summary_has_separate_product_collection_button(client, db_session):
    store = Store(name="Collect Store", url="https://www.aliexpress.com/store/660006")
    product = Product(
        store=store,
        aliexpress_product_id="100500665",
        url="https://www.aliexpress.com/item/100500665.html",
    )
    db_session.add(product)
    db_session.commit()

    response = client.get("/")

    assert response.status_code == 200
    assert f'class="link-button store-collect-products" type="button" data-store-id="{store.id}"' in response.text
    assert '>采集</button><button class="link-button dianxiaomi-store"' in response.text
    assert f'data-product-ids="{product.id}"' in response.text


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
    assert "仅汇总直接读取的商品日增量" in detail.text
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


def test_store_summary_places_confirmed_deactivation_after_dianxiaomi(client, db_session):
    store = Store(name="Removal Candidate", url="https://www.aliexpress.com/store/770004")
    db_session.add(store)
    db_session.commit()

    dashboard = client.get("/").text
    action_group = dashboard.split('class="store-summary-actions"', 1)[1].split("</span>", 1)[0]
    css = client.get("/static/dashboard.css").text
    script = client.get("/static/dashboard.js").text

    assert action_group.index('class="link-button dianxiaomi-store"') < action_group.index('class="link-button danger deactivate-store"')
    assert f'data-store-id="{store.id}"' in action_group
    assert "删除监控店铺" in action_group
    assert ".store-summary-actions { display: inline-flex; align-items: center; justify-self: start;" in css
    handler = script.split('document.querySelectorAll(".deactivate-store")', 1)[1].split(
        'document.querySelectorAll(".deactivate-product")', 1
    )[0]
    assert "window.confirm" in handler
    assert f"/api/stores/${{button.dataset.storeId}}/deactivate" in handler
    assert "商品及历史数据会保留" in handler


def test_monitor_pool_has_manual_review_collection_action(client, db_session):
    store = Store(name="Review Store", url="https://www.aliexpress.com/store/770003")
    product = Product(
        store=store,
        aliexpress_product_id="100500770003",
        url="https://www.aliexpress.com/item/100500770003.html",
        title="Review Candidate",
    )
    db_session.add(product)
    db_session.commit()

    dashboard = client.get("/")
    detail = client.get(f"/dashboard/stores/{store.id}")
    css = client.get("/static/dashboard.css")

    assert dashboard.status_code == detail.status_code == css.status_code == 200
    assert f'class="link-button collect-reviews" type="button" data-product-id="{product.id}">采集评论</button>' in dashboard.text
    assert f'class="link-button collect-reviews" type="button" data-product-id="{product.id}">采集评论</button>' in detail.text
    assert 'class="store-rename-form inline-rename"' in dashboard.text
    assert '.inline-rename[hidden] { display: none; }' in css.text


def test_identity_review_item_exposes_review_evidence_to_dashboard(client):
    script = client.get("/static/dashboard.js").text
    renderer = script.split("function renderBrowserCollectionItem(item)", 1)[1].split(
        "function formatBrowserTime", 1
    )[0]
    handler = script.split('document.querySelector("#browser-collection-status")?.addEventListener("click"', 1)[1].split(
        'document.querySelector("#browser-run-pause")', 1
    )[0]

    assert 'const identityReview = identityDeferred && identityDecision === "pending"' in renderer
    assert "item.identity_review_pending === true" in renderer
    assert "item.identity_review_url" in renderer
    assert "item.identity_review_evidence" in renderer
    assert "ixspy_store_name" in renderer
    assert 'data-browser-item-action="identity-confirm"' in renderer
    assert 'data-browser-item-action="identity-reject"' in renderer
    assert "确定无误" in renderer
    assert "错误" in renderer
    assert "/identity-review/${endpoint}" in handler
    assert "本轮仍保持跳过，需手动重新排队" in handler
    assert "本轮队列不受影响" in handler


def test_identity_deferred_items_have_separate_filter_and_decision_actions(client):
    dashboard = client.get("/").text
    script = client.get("/static/dashboard.js").text
    renderer = script.split("function renderBrowserCollectionItem(item)", 1)[1].split(
        "function formatBrowserTime", 1
    )[0]
    counts = script.split("function renderBrowserCollectionFilter()", 1)[1].split(
        "function renderBrowserCollectionItem", 1
    )[0]

    assert 'data-browser-filter="identity"' in dashboard
    assert "身份审核" in dashboard
    identity_helper = script.split("function isIdentityDeferred(item)", 1)[1].split(
        "function renderBrowserCollectionItem", 1
    )[0]
    assert '"store_identity_deferred"' in identity_helper
    assert 'identityDecision === "confirmed"' in renderer
    assert 'identityDecision === "rejected"' in renderer
    assert 'data-browser-item-action="identity-confirm"' in renderer
    assert 'data-browser-item-action="identity-reject"' in renderer
    assert 'data-browser-item-action="retry"' in renderer
    assert 'browserCollectionFilter === "identity"' in counts
    assert "isIdentityDeferred(item)).length" in counts
    assert "!isIdentityDeferred(item)" in counts


def test_identity_buttons_render_only_for_identity_review_items(client):
    script = client.get("/static/dashboard.js").text
    renderer = script.split("function renderBrowserCollectionItem(item)", 1)[1].split(
        "function formatBrowserTime", 1
    )[0]
    css = client.get("/static/dashboard.css").text

    assert 'const identityReview = identityDeferred && identityDecision === "pending"' in renderer
    assert "item.identity_review_pending === true" in renderer
    assert '.browser-identity-actions .identity-confirm { background:' in css
    assert '.browser-identity-actions .identity-reject { background:' in css
    assert 'color: #111' in css
    assert "待身份复核" in renderer
    assert "const platformVerification = needsVerification && !identityReview" in script
    template = Path("app/templates/dashboard.html").read_text(encoding="utf-8")
    assert 'browser_collection_run.paused_reason == "store_identity_review"' in template


def test_dianxiaomi_product_mismatch_has_confirm_and_reject_actions(client):
    script = client.get("/static/dashboard.js").text
    renderer = script.split("function renderDianxiaomiItem(item)", 1)[1].split(
        "function formatDashboardDate", 1
    )[0]
    handler = script.split('document.querySelector("#dianxiaomi-items")?.addEventListener("click"', 1)[1].split(
        'document.querySelector("#dianxiaomi-delete-failed")', 1
    )[0]
    css = client.get("/static/dashboard.css").text

    assert "isDianxiaomiProductPageMismatch(item)" in renderer
    assert 'item.error_type === "product_page_mismatch"' in script
    assert "目标商品 ID\\s*(\\d+)\\D+当前商品 ID\\s*(\\d+)" in script
    assert 'data-dianxiaomi-action="identity-confirm"' in renderer
    assert 'data-dianxiaomi-action="identity-reject"' in renderer
    assert "确认商品无误" in renderer
    assert ">错误</button>" in renderer
    assert 'action === "identity-confirm" ? "confirm" : "reject"' in handler
    assert "/identity-review/${decision}" in handler
    assert ".dianxiaomi-identity-actions .identity-confirm { background:" in css
    assert ".dianxiaomi-identity-actions .identity-reject { background:" in css
