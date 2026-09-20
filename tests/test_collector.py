from pathlib import Path

import httpx

from app.collectors import CollectorTarget, HTTPCollector
from app.collectors.http import parse_product_html

FIXTURES = Path(__file__).parent / "fixtures"


def test_parses_public_structured_product_data():
    parsed = parse_product_html((FIXTURES / "product_success.html").read_text(encoding="utf-8"))
    assert parsed["cumulative_sold"] == 1200
    assert str(parsed["price_amount"]) == "12.99"
    assert parsed["price_currency"] == "USD"
    assert parsed["review_count"] == 321
    assert parsed["title"] == "Fixture USB Hub"


def test_collector_returns_partial_when_sales_field_is_missing():
    html = (FIXTURES / "product_partial.html").read_text(encoding="utf-8")
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=html)))
    result = HTTPCollector(client).collect(
        CollectorTarget("1005001", "https://www.aliexpress.com/item/1005001.html")
    )
    assert result.parse_status == "partial"
    assert result.cumulative_sold is None
    assert result.error_type == "missing_cumulative_sold"
    assert result.raw_content == html


def test_collector_returns_failed_result_for_changed_page_structure():
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="<html>changed</html>"))
    )
    result = HTTPCollector(client).collect(
        CollectorTarget("1005001", "https://www.aliexpress.com/item/1005001.html")
    )
    assert result.parse_status == "failed"
    assert result.error_type == "page_structure_unrecognized"
    assert result.raw_content == "<html>changed</html>"


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
