from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.auth import require_authenticated, require_page_auth
from app.dashboard import pricing


def test_monitor_nav_contains_pricing_entry():
    template = Path("app/templates/base.html").read_text(encoding="utf-8")
    assert '<a href="/pricing">产品定价表</a>' in template


def test_pricing_page_and_quote_use_authenticated_narrow_proxy(monkeypatch):
    calls = []

    async def upstream(method, path, body=None):
        calls.append((method, path, body))
        if method == "GET":
            return httpx.Response(200, text="<h1>产品定价表</h1>")
        return httpx.Response(200, json={"status": "QUOTED"})

    monkeypatch.setattr(pricing, "_upstream", upstream)
    app = FastAPI()
    app.include_router(pricing.router)
    app.dependency_overrides[require_page_auth] = lambda: None
    app.dependency_overrides[require_authenticated] = lambda: None
    with TestClient(app) as client:
        page = client.get("/pricing")
        assert page.status_code == 200
        assert "产品定价表" in page.text
        quoted = client.post(
            "/pricing/api/quote",
            headers={"Origin": "http://testserver"},
            json={"sku": "SKU-1"},
        )
        assert quoted.status_code == 200
        assert quoted.json() == {"status": "QUOTED"}
    assert calls[0][:2] == ("GET", "/pricing/workbench")
    assert calls[1][:2] == ("POST", "/pricing/api/quote")


def test_pricing_proxy_rejects_cross_origin_and_non_json(monkeypatch):
    async def upstream(method, path, body=None):
        raise AssertionError("invalid requests must not reach pricing Docker")

    monkeypatch.setattr(pricing, "_upstream", upstream)
    app = FastAPI()
    app.include_router(pricing.router)
    app.dependency_overrides[require_authenticated] = lambda: None
    with TestClient(app) as client:
        assert client.post(
            "/pricing/api/quote",
            headers={"Origin": "https://other.example"},
            json={},
        ).status_code == 403
        assert client.post(
            "/pricing/api/quote",
            headers={"Origin": "http://testserver", "Content-Type": "text/plain"},
            content="x",
        ).status_code == 415
