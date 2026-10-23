from decimal import Decimal

import pytest

from app.api.pricing_schemas import PackagingPreviewInput, PricingQuoteInput
from app.services.pricing import PricingService


@pytest.fixture
def pricing_service():
    return PricingService()


def test_quote_separates_carton_inputs(pricing_service):
    result = pricing_service.quote(
        PricingQuoteInput(product_type="ordinary", purchase_price=Decimal("10"))
    )

    assert result["total_cost"] == Decimal("11.40")
    assert result["formula_price"] == Decimal("23.940")
    assert result["formula_profit"] == Decimal("12.540")


def test_ordinary_overrides_affect_quote_only(pricing_service):
    cartons_before = pricing_service.packaging_options()
    result = pricing_service.quote(
        PricingQuoteInput(
            product_type="ordinary",
            purchase_price=Decimal("10"),
            packaging_fee=Decimal("2"),
            multiplier=Decimal("2.5"),
        )
    )

    assert result["total_cost"] == Decimal("12.40")
    assert result["formula_price"] == Decimal("31.000")
    assert pricing_service.packaging_options() == cartons_before


def test_printed_3d_pla_quote(pricing_service):
    result = pricing_service.quote(
        PricingQuoteInput(
            product_type="printed_3d",
            material="PLA",
            filament_grams=Decimal("10"),
            print_hours=1,
            print_minutes=0,
        )
    )

    assert result["total_cost"] == Decimal("2.050")
    assert result["formula_price"] == Decimal("6.150")
    assert result["formula_profit"] == Decimal("4.100")


def test_printed_3d_petg_quote(pricing_service):
    result = pricing_service.quote(
        PricingQuoteInput(
            product_type="printed_3d",
            material="PETG",
            filament_grams=Decimal("100"),
            print_hours=2,
            print_minutes=0,
        )
    )

    assert result["total_cost"] == Decimal("4.800")
    assert result["formula_price"] == Decimal("16.8000")
    assert result["formula_profit"] == Decimal("12.0000")


def test_manual_price_loss_flags_negative_profit(pricing_service):
    result = pricing_service.quote(
        PricingQuoteInput(
            product_type="printed_3d",
            material="PLA",
            filament_grams=Decimal("10"),
            print_hours=1,
            print_minutes=0,
            final_price=Decimal("1"),
        )
    )

    assert result["manual_profit"] == Decimal("-1.050")
    assert result["negative_profit"] is True


def test_packaging_preview_returns_12_half_for_sample(pricing_service):
    result = pricing_service.preview_packaging(
        PackagingPreviewInput(
            product_weight_g=Decimal("35"),
            length_cm=Decimal("10"),
            width_cm=Decimal("5"),
            height_cm=Decimal("2"),
        )
    )

    assert result["status"] == "PREDICTED"
    assert result["predicted_weight_g"] == Decimal("65")
    assert result["carton"] == {
        "name": "12号半纸箱",
        "length_cm": 13,
        "width_cm": 8,
        "height_cm": 5,
        "recommended_weight_g": 30,
    }


def test_packaging_preview_reports_no_match(pricing_service):
    result = pricing_service.preview_packaging(
        PackagingPreviewInput(
            product_weight_g=Decimal("100"),
            length_cm=Decimal("100"),
            width_cm=Decimal("100"),
            height_cm=Decimal("100"),
        )
    )

    assert result["status"] == "NO_FIT"
    assert result["carton"] is None
    assert result["predicted_weight_g"] is None


def test_packaging_catalog_matches_wps_h_column(pricing_service):
    assert [
        (
            item["name"],
            item["length_cm"],
            item["width_cm"],
            item["height_cm"],
            item["recommended_weight_g"],
        )
        for item in pricing_service.packaging_options()
    ] == [
        ("10号纸箱", 18, 10, 12, 50),
        ("12号纸箱", 13, 8, 9, 35),
        ("10号半纸箱", 18, 10, 6, 40),
        ("12号半纸箱", 13, 8, 5, 30),
        ("9号纸箱", 20, 11, 14, 65),
        ("7号纸箱", 23, 13, 16, 95),
        ("T4飞机盒", 25, 20, 7, 85),
        ("11号纸箱", 15, 9, 11, 50),
        ("6号纸箱", 26, 15, 18, 85),
        ("气泡袋1", 15, 11, 4, 5),
        ("气泡袋2", 13, 15, 4, 10),
    ]
