from decimal import Decimal

from app.core.config import get_settings


def test_user_pricing_api_requires_session(client, monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    get_settings.cache_clear()
    try:
        response = client.post(
            "/api/pricing/quote",
            json={"product_type": "ordinary", "purchase_price": "10"},
        )
    finally:
        get_settings.cache_clear()

    assert response.status_code == 401


def test_internal_pricing_accepts_valid_bearer_token(client, monkeypatch):
    monkeypatch.setenv("PRICING_SERVICE_TOKEN", "internal-test-token")
    get_settings.cache_clear()
    try:
        response = client.post(
            "/api/internal/pricing/quote",
            headers={"Authorization": "Bearer internal-test-token"},
            json={"product_type": "ordinary", "purchase_price": "10"},
        )
    finally:
        get_settings.cache_clear()

    assert response.status_code == 200
    assert Decimal(response.json()["formula_price"]) == Decimal("23.94")


def test_internal_pricing_rejects_invalid_token(client, monkeypatch):
    monkeypatch.setenv("PRICING_SERVICE_TOKEN", "internal-test-token")
    get_settings.cache_clear()
    try:
        response = client.post(
            "/api/internal/pricing/quote",
            headers={"Authorization": "Bearer wrong-token"},
            json={"product_type": "ordinary", "purchase_price": "10"},
        )
    finally:
        get_settings.cache_clear()

    assert response.status_code == 401


def test_quote_returns_same_result_for_user_and_internal_api(client, monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "false")
    monkeypatch.setenv("PRICING_SERVICE_TOKEN", "internal-test-token")
    get_settings.cache_clear()
    try:
        payload = {"product_type": "ordinary", "purchase_price": "10"}
        user_response = client.post("/api/pricing/quote", json=payload)
        internal_response = client.post(
            "/api/internal/pricing/quote",
            headers={"Authorization": "Bearer internal-test-token"},
            json=payload,
        )
    finally:
        get_settings.cache_clear()

    assert user_response.status_code == 200
    assert internal_response.status_code == 200
    assert user_response.json() == internal_response.json()
