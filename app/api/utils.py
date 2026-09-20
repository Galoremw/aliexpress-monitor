import re
from urllib.parse import urlparse, urlunparse

from fastapi import HTTPException, status


PRODUCT_ID_PATTERN = re.compile(r"/item/(\d+)(?:\.html)?", re.IGNORECASE)
STORE_ID_PATTERN = re.compile(r"/store/(\d+)", re.IGNORECASE)


def normalize_aliexpress_url(value: str, resource: str) -> str:
    raw = value.strip()
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if not re.fullmatch(r"(?:[a-z0-9-]+\.)*aliexpress\.[a-z.]+", host):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Only public AliExpress URLs are accepted",
        )
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=422, detail="URL must use HTTP or HTTPS")
    if resource == "product" and not PRODUCT_ID_PATTERN.search(parsed.path):
        raise HTTPException(status_code=422, detail="AliExpress product URL must contain /item/<id>")
    return urlunparse(("https", host, parsed.path.rstrip("/"), "", parsed.query, ""))


def extract_product_id(url: str) -> str:
    match = PRODUCT_ID_PATTERN.search(urlparse(url).path)
    if not match:
        raise HTTPException(status_code=422, detail="Unable to extract AliExpress product ID")
    return match.group(1)


def extract_store_id(url: str) -> str | None:
    match = STORE_ID_PATTERN.search(urlparse(url).path)
    return match.group(1) if match else None


def canonical_product_url(product_id: str) -> str:
    if not product_id.isdigit():
        raise HTTPException(status_code=422, detail="AliExpress product ID must be numeric")
    return f"https://www.aliexpress.com/item/{product_id}.html"
