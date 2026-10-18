"""Authenticated, narrow gateway to the Dianxiaomi Docker pricing workbench."""

from urllib.parse import urlparse
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from app.core.auth import require_authenticated, require_page_auth
from app.core.config import get_settings


router = APIRouter(include_in_schema=False)


def _connection() -> tuple[str, str]:
    settings = get_settings()
    base = (settings.pricing_service_url or "").rstrip("/")
    token = settings.pricing_service_token or ""
    if not base or not token or urlparse(base).scheme not in {"http", "https"}:
        raise HTTPException(status_code=503, detail="产品定价表尚未连接店小秘服务")
    return base, token


async def _upstream(method: str, path: str, body: bytes | None = None) -> httpx.Response:
    base, token = _connection()
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            return await client.request(
                method,
                base + path,
                content=body,
                headers={
                    "Authorization": "Bearer " + token,
                    **({"Content-Type": "application/json"} if body is not None else {}),
                },
            )
    except httpx.RequestError as error:
        raise HTTPException(status_code=503, detail="店小秘定价服务暂时不可用") from error


def _same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    host = request.headers.get("host")
    if not origin or not host or urlparse(origin).netloc.casefold() != host.casefold():
        raise HTTPException(status_code=403, detail="跨站核价请求被拒绝")


async def _proxy_json(request: Request, path: str) -> Response:
    _same_origin(request)
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="仅接受 JSON 请求")
    body = await request.body()
    if len(body) > 32768:
        raise HTTPException(status_code=413, detail="核价请求过大")
    response = await _upstream("POST", path, body)
    return Response(
        content=response.content,
        status_code=response.status_code,
        media_type="application/json",
    )


@router.get("/pricing", response_class=HTMLResponse, dependencies=[Depends(require_page_auth)])
async def pricing_page() -> HTMLResponse:
    response = await _upstream("GET", "/pricing/workbench")
    if response.status_code != 200:
        raise HTTPException(status_code=503, detail="产品定价表暂时不可用")
    return HTMLResponse(response.text, headers={"Cache-Control": "no-store"})


@router.post("/pricing/api/quote", dependencies=[Depends(require_authenticated)])
async def pricing_quote(request: Request) -> Response:
    return await _proxy_json(request, "/pricing/api/quote")


@router.post("/pricing/api/records", dependencies=[Depends(require_authenticated)])
async def pricing_save(request: Request) -> Response:
    return await _proxy_json(request, "/pricing/api/records")


@router.post("/pricing/api/records/{record_id}/confirm", dependencies=[Depends(require_authenticated)])
async def pricing_confirm(record_id: UUID, request: Request) -> Response:
    return await _proxy_json(request, f"/pricing/api/records/{record_id}/confirm")
