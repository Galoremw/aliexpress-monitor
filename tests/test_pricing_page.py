from app.core.config import get_settings


def test_pricing_page_requires_login(client, monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    get_settings.cache_clear()
    try:
        response = client.get("/pricing", follow_redirects=False)
    finally:
        get_settings.cache_clear()

    assert response.status_code == 307
    assert response.headers["location"] == "/login"


def test_pricing_page_defaults_to_ordinary_and_has_packaging_preview(client):
    response = client.get("/pricing")

    assert response.status_code == 200
    assert 'id="productType"' in response.text
    assert '<option value="ordinary" selected>普通商品</option>' in response.text
    assert 'id="productWeight"' in response.text
    assert 'id="predictedCartonName"' in response.text
    assert 'id="cartonCatalog"' in response.text
    assert 'id="packagingFee"' in response.text and 'value="1.40"' in response.text
    assert "店小秘项目 API Token" not in response.text
    assert 'id="savePricingRecord"' not in response.text
    assert "核价记录信息" not in response.text


def test_pricing_navigation_is_present_on_monitor_dashboard(client):
    response = client.get("/")

    assert response.status_code == 200
    assert 'href="/pricing"' in response.text
    assert "产品定价表" in response.text


def test_pricing_browser_uses_session_auth_not_service_token(client):
    response = client.get("/static/pricing.js")

    assert response.status_code == 200
    assert "/api/pricing/quote" in response.text
    assert "/api/pricing/packaging-preview" in response.text
    assert "/api/pricing/cartons" in response.text
    assert 'credentials: "include"' in response.text
    assert "/api/internal/pricing/" not in response.text
    assert "PRICING_SERVICE_TOKEN" not in response.text
