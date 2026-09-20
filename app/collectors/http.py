from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any, Iterable

import httpx

from app.collectors.base import CollectorResult, CollectorTarget
from app.core.config import get_settings


class ScriptExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._in_script = False
        self._parts: list[str] = []
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "script":
            self._in_script = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._in_script:
            self.scripts.append("".join(self._parts).strip())
            self._in_script = False


def _json_objects_from_html(html: str) -> list[Any]:
    parser = ScriptExtractor()
    parser.feed(html)
    objects: list[Any] = []
    for script in parser.scripts:
        candidates = [script]
        first_brace = min(
            (position for position in (script.find("{"), script.find("[")) if position >= 0),
            default=-1,
        )
        last_brace = max(script.rfind("}"), script.rfind("]"))
        if first_brace >= 0 and last_brace > first_brace:
            candidates.append(script[first_brace : last_brace + 1])
        for candidate in candidates:
            try:
                value = json.loads(candidate)
            except (json.JSONDecodeError, TypeError):
                continue
            objects.append(value)
            break
    return objects


def _walk(value: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key), child
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _first_value(objects: list[Any], keys: set[str]) -> Any:
    normalized = {key.lower() for key in keys}
    for obj in objects:
        for key, value in _walk(obj):
            if key.lower() in normalized and value not in (None, ""):
                return value
    return None


def _parse_count(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value).strip().lower().replace(",", "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*([km]?)", text)
    if not match:
        return None
    multiplier = {"": 1, "k": 1_000, "m": 1_000_000}[match.group(2)]
    return max(0, int(float(match.group(1)) * multiplier))


def _parse_decimal(value: Any) -> Decimal | None:
    if isinstance(value, dict):
        value = value.get("value") or value.get("amount")
    if value is None:
        return None
    match = re.search(r"\d+(?:[.,]\d+)?", str(value).replace(",", ""))
    if not match:
        return None
    try:
        return Decimal(match.group(0))
    except InvalidOperation:
        return None


def parse_product_html(html: str) -> dict[str, Any]:
    objects = _json_objects_from_html(html)
    sold = _parse_count(
        _first_value(objects, {"tradeCount", "soldCount", "ordersCount", "formatTradeCount"})
    )
    price = _parse_decimal(
        _first_value(objects, {"price", "salePrice", "minPrice", "formatedActivityPrice"})
    )
    currency = _first_value(objects, {"priceCurrency", "currencyCode", "currency"})
    review_count = _parse_count(
        _first_value(objects, {"reviewCount", "ratingCount", "totalValidNum"})
    )
    title = _first_value(objects, {"name", "subject", "title"})
    return {
        "cumulative_sold": sold,
        "price_amount": price,
        "price_currency": str(currency).upper() if currency else None,
        "review_count": review_count,
        "title": str(title).strip() if title else None,
        "structured_objects": objects,
    }


def _is_platform_challenge(html: str) -> bool:
    lowered = html.lower()
    return (
        "captcha" in lowered
        and ("/punish?" in lowered or "x5secdata" in lowered)
    )


class HTTPCollector:
    name = "http_public_page"
    version = "1.0"

    def __init__(self, client: httpx.Client | None = None) -> None:
        settings = get_settings()
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=settings.collector_timeout_seconds,
            headers={"User-Agent": settings.collector_user_agent},
            follow_redirects=True,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def collect(self, target: CollectorTarget) -> CollectorResult:
        collected_at = datetime.now(timezone.utc)
        try:
            response = self._client.get(target.url)
            response.raise_for_status()
            if _is_platform_challenge(response.text):
                return CollectorResult(
                    collected_at=collected_at,
                    collector_name=self.name,
                    collector_version=self.version,
                    parse_status="failed",
                    source_http_status=response.status_code,
                    raw_payload={
                        "target": {"product_id": target.product_id, "url": target.url},
                        "final_url": str(response.url),
                        "challenge_detected": True,
                    },
                    raw_content=response.text,
                    error_type="platform_challenge",
                    error_message=(
                        "AliExpress returned a platform verification page; "
                        "collection stopped without attempting to bypass it"
                    ),
                )
            parsed = parse_product_html(response.text)
            has_any_field = any(
                parsed[key] is not None
                for key in ("cumulative_sold", "price_amount", "review_count", "title")
            )
            if parsed["cumulative_sold"] is not None:
                parse_status = "success"
                error_type = error_message = None
            elif has_any_field:
                parse_status = "partial"
                error_type = "missing_cumulative_sold"
                error_message = "Public cumulative sold/orders value was not found"
            else:
                parse_status = "failed"
                error_type = "page_structure_unrecognized"
                error_message = "No supported public product fields were found"
            return CollectorResult(
                collected_at=collected_at,
                collector_name=self.name,
                collector_version=self.version,
                parse_status=parse_status,
                cumulative_sold=parsed["cumulative_sold"],
                price_amount=parsed["price_amount"],
                price_currency=parsed["price_currency"],
                review_count=parsed["review_count"],
                title=parsed["title"],
                source_http_status=response.status_code,
                raw_payload={
                    "target": {"product_id": target.product_id, "url": target.url},
                    "final_url": str(response.url),
                    "structured_objects": parsed["structured_objects"],
                },
                raw_content=response.text,
                error_type=error_type,
                error_message=error_message,
            )
        except httpx.HTTPStatusError as exc:
            return CollectorResult(
                collected_at=collected_at,
                collector_name=self.name,
                collector_version=self.version,
                parse_status="failed",
                source_http_status=exc.response.status_code,
                raw_payload={"target": {"product_id": target.product_id, "url": target.url}},
                raw_content=exc.response.text,
                error_type="http_status_error",
                error_message=f"Public page returned HTTP {exc.response.status_code}",
            )
        except httpx.HTTPError as exc:
            return CollectorResult(
                collected_at=collected_at,
                collector_name=self.name,
                collector_version=self.version,
                parse_status="failed",
                raw_payload={"target": {"product_id": target.product_id, "url": target.url}},
                error_type="network_error",
                error_message=str(exc),
            )
