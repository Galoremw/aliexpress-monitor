from decimal import Decimal

import pytest

from app.api.pricing_schemas import PricingQuoteInput
from app.services.pricing import PricingService
from app.services.pricing_repository import PricingRepository


def test_save_record_keeps_input_result_and_rule_version(db_session):
    repository = PricingRepository()
    repository.ensure_rule_versions(db_session)
    payload = PricingQuoteInput(
        product_id="external-123",
        product_name="Sample Product",
        sku="SKU-1",
        product_type="ordinary",
        purchase_price=Decimal("10"),
        final_price=Decimal("25"),
    )
    quote = PricingService().quote(payload)

    saved = repository.save(db_session, payload, quote)
    loaded = repository.get(db_session, saved.record_id)

    assert loaded is not None
    assert loaded.product_id == "external-123"
    assert loaded.product_name == "Sample Product"
    assert loaded.sku == "SKU-1"
    assert loaded.rule_version == quote["rule_version"]
    assert loaded.input_json["purchase_price"] == "10"
    assert Decimal(loaded.result_json["formula_price"]) == Decimal("23.94")
    assert loaded.final_price == Decimal("25.00")
    assert loaded.status == "DRAFT"


def test_save_requires_manual_final_price(db_session):
    repository = PricingRepository()
    repository.ensure_rule_versions(db_session)
    payload = PricingQuoteInput(product_type="ordinary", purchase_price=Decimal("10"))
    quote = PricingService().quote(payload)

    with pytest.raises(ValueError, match="final price"):
        repository.save(db_session, payload, quote)


def test_confirm_is_idempotent(db_session):
    repository = PricingRepository()
    repository.ensure_rule_versions(db_session)
    payload = PricingQuoteInput(
        product_type="ordinary",
        purchase_price=Decimal("10"),
        final_price=Decimal("25"),
    )
    quote = PricingService().quote(payload)
    saved = repository.save(db_session, payload, quote)

    first = repository.confirm(db_session, saved.record_id)
    first_confirmed_at = first.confirmed_at
    second = repository.confirm(db_session, saved.record_id)

    assert second.status == "CONFIRMED"
    assert second.confirmed_at == first_confirmed_at


def test_unknown_rule_version_is_rejected(db_session):
    repository = PricingRepository()
    payload = PricingQuoteInput(
        product_type="ordinary",
        purchase_price=Decimal("10"),
        final_price=Decimal("25"),
    )

    with pytest.raises(ValueError, match="unknown pricing rule version"):
        repository.save(
            db_session,
            payload,
            {
                "rule_version": "missing-rule-version",
                "total_cost": Decimal("11.4"),
                "formula_price": Decimal("23.94"),
            },
        )
