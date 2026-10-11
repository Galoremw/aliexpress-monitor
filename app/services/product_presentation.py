from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit


_TITLE_NOISE = re.compile(
    r"\s+(?:"
    r"\d[\d,.]*(?:\s*[km])?\+?\s*(?:sold|orders?)\b"
    r"|(?:已售|销量)\s*[:：]?\s*\d[\d,.]*"
    r"|(?:HK\$|US\$|CA\$|AU\$|SG\$|NZ\$|USD\s*|HKD\s*|CNY\s*|RMB\s*|EUR\s*|GBP\s*|JPY\s*|KRW\s*|\$|€|£|¥|￥|₩)\s*\d[\d,.]*"
    r"|new shoppers save\b"
    r"|free shipping\b"
    r"|extra\s+\d+(?:\.\d+)?%\s+off\b"
    r").*$",
    re.IGNORECASE,
)


def clean_product_title(value: str | None) -> str | None:
    if not value:
        return value
    title = re.sub(r"\s+", " ", value).strip()
    match = _TITLE_NOISE.search(title)
    if match:
        title = title[: match.start()].rstrip(" -|·,;:")
    return title or value.strip()


_IMAGE_KEYS = (
    "main_image_url",
    "product_image_url",
    "image_url",
    "imageUrl",
    "productImageUrl",
    "productImage",
    "image",
    "images",
    "contentUrl",
    "url",
    "src",
    "structured_objects",
    "product",
    "data",
    "extension_raw_data",
    "products",
    "candidates",
)


def _image_values(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        candidate = value.strip()
        if candidate.startswith(("https://", "http://")):
            yield candidate
        return
    if isinstance(value, list):
        for item in value:
            yield from _image_values(item)
        return
    if isinstance(value, dict):
        for key in _IMAGE_KEYS:
            if key in value:
                yield from _image_values(value[key])
        for key, child in value.items():
            if key not in _IMAGE_KEYS:
                yield from _image_values(child)


def product_image_url(raw_data: Any) -> str | None:
    if not isinstance(raw_data, dict):
        return None

    def usable(candidates: Iterator[str]) -> str | None:
        return next(
            (
                candidate
                for candidate in candidates
                if not re.search(r"(?:avatar|logo|icon)(?:[._/?-]|$)", candidate, re.IGNORECASE)
                and _looks_like_image(candidate)
            ),
            None,
        )

    for key in ("main_image_url", "product_image_url", "image_url"):
        candidate = usable(_image_values(raw_data.get(key)))
        if candidate:
            return candidate
    for key in _IMAGE_KEYS[3:]:
        candidate = usable(_image_values(raw_data.get(key)))
        if candidate:
            return candidate
    return None


def _looks_like_image(value: str) -> bool:
    path = urlsplit(value).path.lower()
    return "/kf/" in path or bool(
        re.search(r"\.(?:avif|gif|jpe?g|png|webp|bmp|svg)(?:_|$)", path)
    )
