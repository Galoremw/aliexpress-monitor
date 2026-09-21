from datetime import date, datetime, timezone
from subprocess import CompletedProcess

import httpx

from app.collectors.store_dependencies import get_store_discovery_collector
from app.collectors.store_discovery import (
    HTTPStoreDiscoveryCollector,
    FirecrawlStoreDiscoveryCollector,
    FallbackStoreDiscoveryCollector,
    StoreDiscoveryResult,
    StoreProductCandidate,
    parse_store_products,
)
from app.db.models import Product, ProductDailyMetric, ProductSnapshot, StoreSnapshot
from app.core.config import get_settings


def candidates(count=25):
    return [
        StoreProductCandidate(
            product_id=str(100500900000 + index),
            url=f"https://www.aliexpress.com/item/{100500900000 + index}.html",
            title=f"Discovered Product {index}",
            public_cumulative_sold=index * 100,
        )
        for index in range(count)
    ]


class StubStoreCollector:
    name = "store_fixture"
    version = "1"

    def discover(self, store_url):
        return StoreDiscoveryResult(
            collected_at=datetime(2026, 9, 21, 1, tzinfo=timezone.utc),
            collector_name=self.name,
            collector_version=self.version,
            parse_status="success",
            candidates=candidates(),
            source_http_status=200,
            raw_payload={"fixture": True},
            raw_content="<html>store fixture</html>",
        )


def create_store(client):
    return client.post(
        "/api/stores",
        json={"name": "Discovery Store", "url": "https://www.aliexpress.com/store/990001"},
    ).json()


def test_parses_and_ranks_public_store_product_links():
    html = "<html>" + "".join(
        f'<a href="/item/{100500800000 + index}.html" title="Item {index}">{index}K sold</a>'
        for index in range(1, 4)
    ) + "</html>"
    parsed, _ = parse_store_products(html, "https://www.aliexpress.com/store/990001")
    assert [item.public_cumulative_sold for item in parsed] == [3000, 2000, 1000]
    assert parsed[0].url == "https://www.aliexpress.com/item/100500800003.html"


def test_http_store_collector_saves_failure_context():
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="<html>changed</html>"))
    )
    result = HTTPStoreDiscoveryCollector(client).discover(
        "https://www.aliexpress.com/store/990001"
    )
    assert result.parse_status == "failed"
    assert result.error_type == "store_page_structure_unrecognized"
    assert result.raw_content == "<html>changed</html>"


def test_firecrawl_api_adapter_parses_rendered_links(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    get_settings.cache_clear()
    payload = {
        "success": True,
        "data": {
            "rawHtml": '<a href="https://www.aliexpress.com/item/100500812345.html">42 sold</a>',
            "links": ["https://www.aliexpress.com/item/100500812345.html"],
            "metadata": {"statusCode": 200, "cacheState": "miss"},
        },
    }
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    )
    collector = FirecrawlStoreDiscoveryCollector(client)

    result = collector.discover("https://www.aliexpress.com/store/990001")

    assert result.parse_status == "success"
    assert result.candidates[0].product_id == "100500812345"
    assert result.candidates[0].public_cumulative_sold == 42
    assert result.raw_payload["metadata"]["cacheState"] == "miss"
    get_settings.cache_clear()


def test_firecrawl_cli_invokes_node_and_preserves_query_string(monkeypatch, tmp_path):
    shim_dir = tmp_path / "bin"
    nested_dir = tmp_path / "tools" / "node_modules" / ".bin"
    entry = tmp_path / "tools" / "node_modules" / "firecrawl-cli" / "dist" / "index.js"
    shim_dir.mkdir(parents=True)
    nested_dir.mkdir(parents=True)
    entry.parent.mkdir(parents=True)
    entry.write_text("", encoding="utf-8")
    nested_shim = nested_dir / "firecrawl.CMD"
    nested_shim.write_text("", encoding="utf-8")
    shim = shim_dir / "firecrawl.CMD"
    shim.write_text(f'@CALL "{nested_shim}" %*', encoding="utf-8")

    monkeypatch.setattr("app.collectors.store_discovery.os.name", "nt")
    monkeypatch.setattr(
        "app.collectors.store_discovery.shutil.which",
        lambda command: str(shim) if command == "firecrawl.CMD" else "C:/node.exe",
    )
    observed = {}

    def fake_run(args, **kwargs):
        observed["args"] = args
        observed["kwargs"] = kwargs
        return CompletedProcess(args, 0, stdout='{"data":{"links":[]}}', stderr="")

    monkeypatch.setattr("app.collectors.store_discovery.subprocess.run", fake_run)
    collector = FirecrawlStoreDiscoveryCollector()
    url = "https://www.aliexpress.com/store/123?spm=test&sortType=orders_desc"

    collector._scrape_via_cli(url)

    assert observed["args"][:2] == ["C:/node.exe", str(entry)]
    assert observed["args"][3] == url
    assert observed["kwargs"]["encoding"] == "utf-8"
    assert observed["kwargs"]["errors"] == "replace"


def test_firecrawl_cli_empty_output_is_a_failed_result(monkeypatch):
    collector = FirecrawlStoreDiscoveryCollector()
    monkeypatch.setattr(collector, "_cli_command", "firecrawl")
    monkeypatch.setattr(
        "app.collectors.store_discovery.subprocess.run",
        lambda *args, **kwargs: CompletedProcess(args[0], 0, stdout=None, stderr=None),
    )

    result = collector.discover("https://www.aliexpress.com/store/123")

    assert result.parse_status == "failed"
    assert result.error_type == "firecrawl_error"
    assert result.error_message == "Firecrawl CLI returned no JSON output"


def test_fallback_uses_firecrawl_only_after_http_finds_no_products():
    failed = StoreDiscoveryResult(
        collected_at=datetime.now(timezone.utc),
        collector_name="http",
        collector_version="1",
        parse_status="failed",
        raw_payload={"attempt": "http"},
    )
    succeeded = StoreDiscoveryResult(
        collected_at=datetime.now(timezone.utc),
        collector_name="firecrawl",
        collector_version="1",
        parse_status="success",
        candidates=candidates(1),
    )

    class Stub:
        available = True

        def __init__(self, result):
            self.result = result
            self.calls = 0

        def discover(self, store_url):
            self.calls += 1
            return self.result

    primary, fallback = Stub(failed), Stub(succeeded)
    result = FallbackStoreDiscoveryCollector(primary, fallback).discover(
        "https://www.aliexpress.com/store/990001"
    )
    assert result.parse_status == "success"
    assert primary.calls == 1
    assert fallback.calls == 1


def test_store_discovery_adds_products_and_returns_first_twenty_links(client, db_session):
    store = create_store(client)
    client.app.dependency_overrides[get_store_discovery_collector] = lambda: StubStoreCollector()

    response = client.post(f"/api/stores/{store['id']}/discover?limit=25")

    assert response.status_code == 200
    body = response.json()
    assert body["parse_status"] == "success"
    assert body["discovered_count"] == 25
    assert body["added_count"] == 25
    assert len(body["product_links"]) == 20
    products = client.get(f"/api/products?store_id={store['id']}").json()
    assert len(products) == 25
    assert all(product["discovery_source"] == "store_page" for product in products)
    snapshot = db_session.get(StoreSnapshot, body["snapshot_id"])
    assert snapshot.raw_payload == {"fixture": True}


def test_store_discovery_default_monitors_top_twenty(client):
    store = create_store(client)
    client.app.dependency_overrides[get_store_discovery_collector] = lambda: StubStoreCollector()

    response = client.post(f"/api/stores/{store['id']}/discover")

    assert response.status_code == 200
    body = response.json()
    assert body["discovered_count"] == 25
    assert body["added_count"] == 20
    products = client.get(f"/api/products?store_id={store['id']}").json()
    assert len(products) == 20
    assert products[0]["discovery_rank"] == 1
    assert products[-1]["discovery_rank"] == 20


def test_store_discovery_refreshes_auto_pool_and_keeps_snapshot_history(client, db_session):
    store = create_store(client)
    replacement = StoreProductCandidate(
        product_id="100500999999",
        url="https://www.aliexpress.com/item/100500999999.html",
        title="Replacement Product",
        public_cumulative_sold=9999,
    )

    class SequenceCollector:
        name = "sequence_fixture"
        version = "1"

        def __init__(self):
            self.results = [
                candidates(20),
                candidates(20)[1:] + [replacement],
                candidates(20),
            ]

        def discover(self, store_url):
            return StoreDiscoveryResult(
                collected_at=datetime.now(timezone.utc),
                collector_name=self.name,
                collector_version=self.version,
                parse_status="success",
                candidates=self.results.pop(0),
                source_http_status=200,
            )

    collector = SequenceCollector()
    client.app.dependency_overrides[get_store_discovery_collector] = lambda: collector

    for _ in range(3):
        response = client.post(f"/api/stores/{store['id']}/discover")
        assert response.status_code == 200

    products = {
        product.aliexpress_product_id: product
        for product in db_session.query(Product).filter_by(store_id=store["id"])
    }
    assert products["100500900000"].status == "active"
    assert products["100500999999"].status == "inactive"
    assert len(db_session.query(ProductSnapshot).filter_by(source="AUTO").all()) == 60


def test_yesterday_top_products_returns_only_best_twenty(client, db_session):
    store = create_store(client)
    for index, candidate in enumerate(candidates(), start=1):
        product = Product(
            store_id=store["id"],
            aliexpress_product_id=candidate.product_id,
            url=candidate.url,
            title=candidate.title,
            discovery_source="store_page",
            discovery_rank=index,
        )
        db_session.add(product)
        db_session.flush()
        db_session.add(
            ProductDailyMetric(
                product_id=product.id,
                metric_date=date(2026, 9, 20),
                estimated_sales=index,
                is_estimable=True,
                reason="estimated_from_public_cumulative_delta",
            )
        )
    db_session.commit()

    response = client.get(
        f"/api/stores/{store['id']}/top-products?metric_date=2026-09-20&limit=20"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert len(body["products"]) == 20
    assert body["products"][0]["estimated_sales"] == 25
    assert body["products"][-1]["estimated_sales"] == 6
    assert body["estimate_type"] == "estimated"


def test_top_products_waits_for_daily_baseline(client):
    store = create_store(client)
    response = client.get(f"/api/stores/{store['id']}/top-products")
    assert response.status_code == 200
    assert response.json()["status"] == "awaiting_daily_baseline"
    assert response.json()["products"] == []
