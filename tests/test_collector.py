from pathlib import Path

import httpx

from app.collectors import CollectorTarget, HTTPCollector
from app.collectors.http import parse_product_html

FIXTURES = Path(__file__).parent / "fixtures"


def test_parses_public_structured_product_data():
    parsed = parse_product_html((FIXTURES / "product_success.html").read_text(encoding="utf-8"))
    assert parsed["cumulative_sold"] is None
    assert str(parsed["price_amount"]) == "12.99"
    assert parsed["price_currency"] == "USD"
    assert parsed["review_count"] == 321
    assert parsed["title"] == "Fixture USB Hub"


def test_parses_clean_title_and_main_image_from_public_product_jsonld():
    html = '''<script type="application/ld+json">
      {"@type":"Product","name":"Desk Organizer 120 sold $8.50",
       "image":["https://ae01.alicdn.com/kf/main-image.jpg"]}
    </script>'''

    parsed = parse_product_html(html)

    assert parsed["title"] == "Desk Organizer"
    assert parsed["main_image_url"] == "https://ae01.alicdn.com/kf/main-image.jpg"


def test_parses_main_image_from_public_open_graph_metadata():
    html = '''<html><head><meta property="og:image" content="https://ae01.alicdn.com/kf/og-main.jpg"></head>
      <body><script type="application/ld+json">{"@type":"Product","name":"Desk Organizer"}</script></body></html>'''

    parsed = parse_product_html(html)

    assert parsed["main_image_url"] == "https://ae01.alicdn.com/kf/og-main.jpg"


def test_collector_persists_public_open_graph_image_in_snapshot_payload():
    html = '''<html><head><meta property="og:image" content="https://ae01.alicdn.com/kf/og-main.jpg"></head>
      <body>Public product summary with enough page text to be a partial observation.</body></html>'''
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=html)))

    result = HTTPCollector(client).collect(
        CollectorTarget("1005001", "https://www.aliexpress.com/item/1005001.html")
    )

    assert result.parse_status == "partial"
    assert result.raw_payload["main_image_url"] == "https://ae01.alicdn.com/kf/og-main.jpg"


def test_collector_returns_partial_when_sales_field_is_missing():
    html = (FIXTURES / "product_partial.html").read_text(encoding="utf-8")
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=html)))
    result = HTTPCollector(client).collect(
        CollectorTarget("1005001", "https://www.aliexpress.com/item/1005001.html")
    )
    assert result.parse_status == "partial"
    assert result.cumulative_sold is None
    assert result.error_type == "missing_ixspy_daily_increment"
    assert result.raw_content is None


def test_collector_returns_failed_result_for_changed_page_structure():
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="<html>changed</html>"))
    )
    result = HTTPCollector(client).collect(
        CollectorTarget("1005001", "https://www.aliexpress.com/item/1005001.html")
    )
    assert result.parse_status == "failed"
    assert result.error_type == "page_structure_unrecognized"
    assert result.raw_content is None


def test_collector_captures_http_failure_without_retry_or_bypass():
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(403, text="blocked"))
    )
    result = HTTPCollector(client).collect(
        CollectorTarget("1005001", "https://www.aliexpress.com/item/1005001.html")
    )
    assert result.parse_status == "failed"
    assert result.source_http_status == 403
    assert result.error_type == "http_status_error"
    assert result.raw_content == "blocked"


def test_collector_identifies_platform_challenge_without_bypass():
    html = '<script>window._config_={"action":"captcha","url":"/punish?x5secdata=test"}</script>'
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=html))
    )

    result = HTTPCollector(client).collect(
        CollectorTarget("1005001", "https://www.aliexpress.com/item/1005001.html")
    )

    assert result.parse_status == "failed"
    assert result.error_type == "platform_challenge"
    assert result.raw_payload["challenge_detected"] is True
    assert "without attempting to bypass" in result.error_message
