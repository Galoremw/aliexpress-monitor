import re
from urllib.parse import urlsplit


_STORE_ORDERS_PATH = re.compile(r"^/store/([0-9]+)(?:/pages/all-items\.html)?$")
_ALIEXPRESS_HOST = "aliexpress.com"


def normalize_store_orders_identity(page_url: str) -> tuple[str, str]:
    """Return a canonical Orders identity for an AliExpress store or Orders URL.

    AliExpress can rewrite the Orders listing URL to the store root while leaving
    the same listing rendered. Normalize both routes to one identity so a manually
    approved store URL remains usable after that rewrite.
    """
    if not isinstance(page_url, str) or not page_url or page_url != page_url.strip():
        raise ValueError("A valid HTTPS AliExpress Orders URL is required")

    try:
        parsed = urlsplit(page_url)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("A valid HTTPS AliExpress Orders URL is required") from exc

    if (
        parsed.scheme.lower() != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise ValueError("A valid HTTPS AliExpress Orders URL is required")

    normalized_host = host.lower()
    if normalized_host != _ALIEXPRESS_HOST and not normalized_host.endswith(
        f".{_ALIEXPRESS_HOST}"
    ):
        raise ValueError("URL host is not AliExpress")

    match = _STORE_ORDERS_PATH.fullmatch(parsed.path)
    if not match:
        raise ValueError("URL is not an AliExpress store Orders page")

    route_store_id = match.group(1)
    normalized_path = f"/store/{route_store_id}/pages/all-items.html"
    return route_store_id, f"{normalized_host}{normalized_path}"
