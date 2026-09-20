from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Iterable, Protocol
from urllib.parse import urljoin, urlparse

import httpx

from app.collectors.http import _json_objects_from_html, _parse_count
from app.core.config import get_settings


@dataclass(frozen=True, slots=True)
class StoreProductCandidate:
    product_id: str
    url: str
    title: str | None = None
    public_cumulative_sold: int | None = None


@dataclass(frozen=True, slots=True)
class StoreDiscoveryResult:
    collected_at: datetime
    collector_name: str
    collector_version: str
    parse_status: str
    candidates: list[StoreProductCandidate] = field(default_factory=list)
    source_http_status: int | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)
    raw_content: str | None = None
    error_type: str | None = None
    error_message: str | None = None


class StoreDiscoveryCollector(Protocol):
    name: str
    version: str

    def discover(self, store_url: str) -> StoreDiscoveryResult: ...


class ProductLinkParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self._current: dict[str, Any] | None = None
        self.links: list[StoreProductCandidate] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attributes = dict(attrs)
        href = attributes.get("href") or ""
        match = re.search(r"/item/(\d+)(?:\.html)?", href, re.IGNORECASE)
        if not match:
            return
        self._current = {
            "product_id": match.group(1),
            "url": urljoin(self.base_url, href),
            "title": attributes.get("title") or attributes.get("aria-label"),
            "text": [],
        }

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._current["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._current is None:
            return
        text = " ".join(self._current["text"])
        title = self._current["title"] or re.sub(r"\s+", " ", text).strip() or None
        sold = None
        sold_match = re.search(r"([\d.,]+\s*[KM]?)\s*(?:sold|orders?)", text, re.IGNORECASE)
        if sold_match:
            sold = _parse_count(sold_match.group(1))
        self.links.append(
            StoreProductCandidate(
                product_id=self._current["product_id"],
                url=self._current["url"],
                title=title,
                public_cumulative_sold=sold,
            )
        )
        self._current = None


def _dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _dicts(child)


def _candidate_from_dict(value: dict[str, Any], base_url: str) -> StoreProductCandidate | None:
    product_id = next(
        (value.get(key) for key in ("productId", "itemId", "product_id", "item_id") if value.get(key)),
        None,
    )
    product_url = next(
        (value.get(key) for key in ("productUrl", "productLink", "itemUrl", "url") if value.get(key)),
        None,
    )
    if product_url and not product_id:
        match = re.search(r"/item/(\d+)(?:\.html)?", str(product_url), re.IGNORECASE)
        product_id = match.group(1) if match else None
    if not product_id or not str(product_id).isdigit():
        return None
    absolute_url = (
        urljoin(base_url, str(product_url))
        if product_url
        else f"https://www.aliexpress.com/item/{product_id}.html"
    )
    host = (urlparse(absolute_url).hostname or "").lower()
    if not re.fullmatch(r"(?:[a-z0-9-]+\.)*aliexpress\.[a-z.]+", host):
        return None
    title = next(
        (value.get(key) for key in ("title", "productTitle", "subject", "name") if value.get(key)),
        None,
    )
    sold_value = next(
        (value.get(key) for key in ("tradeCount", "soldCount", "ordersCount", "formatTradeCount") if value.get(key) is not None),
        None,
    )
    return StoreProductCandidate(
        product_id=str(product_id),
        url=absolute_url,
        title=str(title).strip() if title else None,
        public_cumulative_sold=_parse_count(sold_value),
    )


def parse_store_products(html: str, store_url: str) -> tuple[list[StoreProductCandidate], list[Any]]:
    parser = ProductLinkParser(store_url)
    parser.feed(html)
    structured_objects = _json_objects_from_html(html)
    candidates = list(parser.links)
    for obj in structured_objects:
        for value in _dicts(obj):
            candidate = _candidate_from_dict(value, store_url)
            if candidate:
                candidates.append(candidate)

    unique: dict[str, StoreProductCandidate] = {}
    for candidate in candidates:
        existing = unique.get(candidate.product_id)
        if existing is None or (
            existing.public_cumulative_sold is None
            and candidate.public_cumulative_sold is not None
        ):
            unique[candidate.product_id] = candidate
    ordered = sorted(
        unique.values(),
        key=lambda item: (
            item.public_cumulative_sold is not None,
            item.public_cumulative_sold or 0,
        ),
        reverse=True,
    )
    return ordered, structured_objects


class HTTPStoreDiscoveryCollector:
    name = "http_public_store_page"
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

    def discover(self, store_url: str) -> StoreDiscoveryResult:
        collected_at = datetime.now(timezone.utc)
        try:
            response = self._client.get(store_url)
            response.raise_for_status()
            candidates, structured_objects = parse_store_products(response.text, store_url)
            return StoreDiscoveryResult(
                collected_at=collected_at,
                collector_name=self.name,
                collector_version=self.version,
                parse_status="success" if candidates else "failed",
                candidates=candidates,
                source_http_status=response.status_code,
                raw_payload={
                    "store_url": store_url,
                    "final_url": str(response.url),
                    "candidate_ids": [item.product_id for item in candidates],
                    "structured_objects": structured_objects,
                },
                raw_content=response.text,
                error_type=None if candidates else "store_page_structure_unrecognized",
                error_message=None if candidates else "No public product links were found on the store page",
            )
        except httpx.HTTPStatusError as exc:
            return StoreDiscoveryResult(
                collected_at=collected_at,
                collector_name=self.name,
                collector_version=self.version,
                parse_status="failed",
                source_http_status=exc.response.status_code,
                raw_payload={"store_url": store_url},
                raw_content=exc.response.text,
                error_type="http_status_error",
                error_message=f"Public store page returned HTTP {exc.response.status_code}",
            )
        except httpx.HTTPError as exc:
            return StoreDiscoveryResult(
                collected_at=collected_at,
                collector_name=self.name,
                collector_version=self.version,
                parse_status="failed",
                raw_payload={"store_url": store_url},
                error_type="network_error",
                error_message=str(exc),
            )


class FirecrawlStoreDiscoveryCollector:
    name = "firecrawl_public_store_page"
    version = "1.0"

    def __init__(self, client: httpx.Client | None = None) -> None:
        settings = get_settings()
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=60.0)
        self._cli_command = shutil.which("firecrawl.CMD") or shutil.which("firecrawl")

    def _cli_prefix(self) -> list[str]:
        if not self._cli_command:
            raise RuntimeError("Firecrawl CLI is not installed")
        if os.name != "nt" or not self._cli_command.lower().endswith(".cmd"):
            return [self._cli_command]

        # Calling a .CMD shim through subprocess lets cmd.exe reinterpret '&' in
        # AliExpress query strings. Resolve pnpm's JS entry and invoke Node directly.
        shim = open(self._cli_command, encoding="utf-8").read()
        match = re.search(r'CALL\s+"([^"]+firecrawl\.CMD)"', shim, re.IGNORECASE)
        nested_shim = match.group(1) if match else self._cli_command
        nested_dir = os.path.dirname(nested_shim)
        entry = os.path.normpath(os.path.join(nested_dir, "..", "firecrawl-cli", "dist", "index.js"))
        node = shutil.which("node")
        if not node or not os.path.isfile(entry):
            raise RuntimeError("Unable to resolve the Firecrawl CLI Node entry point")
        return [node, entry]

    @property
    def available(self) -> bool:
        return bool(self._settings.firecrawl_api_key or self._cli_command)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _scrape_via_api(self, store_url: str) -> dict[str, Any]:
        response = self._client.post(
            f"{self._settings.firecrawl_api_url.rstrip('/')}/v2/scrape",
            headers={
                "Authorization": f"Bearer {self._settings.firecrawl_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "url": store_url,
                "formats": ["rawHtml", "links"],
                "onlyMainContent": False,
                "waitFor": self._settings.firecrawl_wait_for_ms,
                "maxAge": 0,
            },
        )
        response.raise_for_status()
        return response.json()

    def _scrape_via_cli(self, store_url: str) -> dict[str, Any]:
        completed = subprocess.run(
            [
                *self._cli_prefix(),
                "scrape",
                store_url,
                "--format",
                "rawHtml,links",
                "--json",
                "--wait-for",
                str(self._settings.firecrawl_wait_for_ms),
                "--max-age",
                "0",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout).strip()[:500])
        if not completed.stdout or not completed.stdout.strip():
            raise RuntimeError("Firecrawl CLI returned no JSON output")
        payload = json.loads(completed.stdout)
        if not isinstance(payload, dict):
            raise RuntimeError("Firecrawl CLI returned an unexpected JSON payload")
        return payload

    def discover(self, store_url: str) -> StoreDiscoveryResult:
        collected_at = datetime.now(timezone.utc)
        try:
            payload = (
                self._scrape_via_api(store_url)
                if self._settings.firecrawl_api_key
                else self._scrape_via_cli(store_url)
            )
            data = payload.get("data", payload)
            html = data.get("rawHtml") or data.get("raw_html") or data.get("html") or ""
            links = data.get("links") or []
            link_markup = "".join(f'<a href="{link}"></a>' for link in links if isinstance(link, str))
            candidates, structured_objects = parse_store_products(
                f"{html}{link_markup}", store_url
            )
            metadata = data.get("metadata") or {}
            return StoreDiscoveryResult(
                collected_at=collected_at,
                collector_name=self.name,
                collector_version=self.version,
                parse_status="success" if candidates else "failed",
                candidates=candidates,
                source_http_status=metadata.get("statusCode") or metadata.get("status_code"),
                raw_payload={
                    "store_url": store_url,
                    "candidate_ids": [item.product_id for item in candidates],
                    "links": links,
                    "metadata": metadata,
                    "structured_objects": structured_objects,
                },
                raw_content=html,
                error_type=None if candidates else "firecrawl_store_page_structure_unrecognized",
                error_message=None if candidates else "Firecrawl returned no public product links",
            )
        except (
            httpx.HTTPError,
            json.JSONDecodeError,
            OSError,
            RuntimeError,
            subprocess.SubprocessError,
            TypeError,
        ) as exc:
            return StoreDiscoveryResult(
                collected_at=collected_at,
                collector_name=self.name,
                collector_version=self.version,
                parse_status="failed",
                raw_payload={"store_url": store_url},
                error_type="firecrawl_error",
                error_message=str(exc),
            )


class FallbackStoreDiscoveryCollector:
    name = "public_store_page_fallback"
    version = "1.0"

    def __init__(
        self,
        primary: StoreDiscoveryCollector,
        fallback: FirecrawlStoreDiscoveryCollector | None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback

    def close(self) -> None:
        for collector in (self.primary, self.fallback):
            close = getattr(collector, "close", None)
            if close:
                close()

    def discover(self, store_url: str) -> StoreDiscoveryResult:
        primary_result = self.primary.discover(store_url)
        if primary_result.candidates or self.fallback is None or not self.fallback.available:
            return primary_result
        fallback_result = self.fallback.discover(store_url)
        if not fallback_result.candidates:
            return StoreDiscoveryResult(
                collected_at=fallback_result.collected_at,
                collector_name=fallback_result.collector_name,
                collector_version=fallback_result.collector_version,
                parse_status="failed",
                candidates=[],
                source_http_status=fallback_result.source_http_status,
                raw_payload={
                    "http_attempt": primary_result.raw_payload,
                    "firecrawl_attempt": fallback_result.raw_payload,
                },
                raw_content=fallback_result.raw_content or primary_result.raw_content,
                error_type=fallback_result.error_type,
                error_message=fallback_result.error_message,
            )
        return fallback_result
