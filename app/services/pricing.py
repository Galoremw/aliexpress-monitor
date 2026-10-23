"""Versioned pricing and independent packaging prediction."""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.api.pricing_schemas import PricingQuoteInput, ProductType


RULES_DIR = Path(__file__).resolve().parents[1] / "pricing_rules"
ORDINARY_RULE_PATH = RULES_DIR / "ordinary_v2.json"
PRINTED_3D_RULE_PATH = RULES_DIR / "printed_3d_v1.json"


class PricingService:
    def __init__(
        self,
        ordinary_rule_path: Path = ORDINARY_RULE_PATH,
        printed_3d_rule_path: Path = PRINTED_3D_RULE_PATH,
    ) -> None:
        self.ordinary_rule = self._read_rule(ordinary_rule_path)
        self.printed_3d_rule = self._read_rule(printed_3d_rule_path)
        self.allowance_cm = Decimal(str(self.ordinary_rule["parameters"]["D10"]))

    @staticmethod
    def _read_rule(path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    def quote(self, payload: PricingQuoteInput) -> dict[str, Any]:
        if payload.product_type is ProductType.PRINTED_3D:
            return self._quote_printed_3d(payload)
        return self._quote_ordinary(payload)

    def _quote_ordinary(self, payload: PricingQuoteInput) -> dict[str, Any]:
        rule = self.ordinary_rule
        base: dict[str, Any] = {
            "status": "MISSING_DATA",
            "rule_version": rule["version"],
            "source_sheet": rule["sheet"],
            "product_type": ProductType.ORDINARY.value,
            "missing": [],
        }
        if payload.purchase_price is None:
            return {**base, "missing": ["请填写供货成本"]}

        parameters = rule["parameters"]
        package_fee = payload.packaging_fee
        if package_fee is None:
            package_fee = Decimal(str(parameters["K4"]))
        multiplier = payload.multiplier
        if multiplier is None:
            multiplier = Decimal(str(parameters["D3"]))
        # Ordinary freight remains the fixed WPS L4 value. Product dimensions
        # and weight belong only to preview_packaging and cannot affect quotes.
        shipping_fee = Decimal(str(rule["sample"]["L4"]))
        total_cost = payload.purchase_price + package_fee + shipping_fee
        formula_price = total_cost * multiplier
        formula_profit = formula_price - total_cost
        manual_profit = (
            payload.final_price - total_cost if payload.final_price is not None else None
        )
        return {
            **base,
            "status": "QUOTED",
            "purchase_price": payload.purchase_price,
            "packaging_fee": package_fee,
            "shipping_fee": shipping_fee,
            "shipping_fee_source": "fixed_default",
            "multiplier": multiplier,
            "total_cost": total_cost,
            "cost_price": total_cost,
            "formula_price": formula_price,
            "formula_profit": formula_profit,
            "formula_profit_sign": self._profit_sign(formula_profit),
            "final_price": payload.final_price,
            "manual_profit": manual_profit,
            "manual_profit_sign": self._profit_sign(manual_profit),
            "negative_profit": manual_profit is not None and manual_profit < 0,
            "manual_overrides": {
                "packaging_fee": payload.packaging_fee is not None,
                "multiplier": payload.multiplier is not None,
                "final_price": payload.final_price is not None,
            },
            "missing": [],
            "warning": rule["source_note"],
        }

    def _quote_printed_3d(self, payload: PricingQuoteInput) -> dict[str, Any]:
        rule = self.printed_3d_rule
        missing: list[str] = []
        if payload.material is None:
            missing.append("请选择打印材质 PLA 或 PETG")
        if payload.filament_grams is None:
            missing.append("请填写耗材克数")
        if payload.print_hours is None:
            missing.append("请填写打印小时数")
        if payload.print_minutes is None:
            missing.append("请填写打印分钟数")
        base: dict[str, Any] = {
            "status": "MISSING_DATA" if missing else "QUOTED",
            "rule_version": rule["version"],
            "source_sheet": rule["sheet"],
            "product_type": ProductType.PRINTED_3D.value,
            "missing": missing,
        }
        if missing:
            return base

        assert payload.material is not None
        assert payload.filament_grams is not None
        assert payload.print_hours is not None
        assert payload.print_minutes is not None
        material_rule = rule["materials"][payload.material.value]
        duration_hours = Decimal(payload.print_hours) + Decimal(payload.print_minutes) / 60
        material_cost = (
            payload.filament_grams
            * Decimal(str(material_rule["material_cost_per_kg"]))
            / 1000
        )
        electricity_cost = duration_hours * Decimal(
            str(material_rule["electricity_cost_per_hour"])
        )
        production_cost = material_cost + electricity_cost
        fixed_packaging_shipping = Decimal(str(rule["fixed_packaging_shipping"]))
        total_cost = production_cost + fixed_packaging_shipping
        multiplier = Decimal(str(material_rule["multiplier"]))
        formula_price = total_cost * multiplier
        formula_profit = formula_price - total_cost
        manual_profit = (
            payload.final_price - total_cost if payload.final_price is not None else None
        )
        return {
            **base,
            "material": payload.material.value,
            "filament_grams": payload.filament_grams,
            "print_hours": payload.print_hours,
            "print_minutes": payload.print_minutes,
            "duration_hours": duration_hours,
            "material_cost": material_cost,
            "electricity_cost": electricity_cost,
            "production_cost": production_cost,
            "packaging_shipping": fixed_packaging_shipping,
            "total_cost": total_cost,
            "cost_price": total_cost,
            "multiplier": multiplier,
            "formula_price": formula_price,
            "formula_profit": formula_profit,
            "formula_profit_sign": self._profit_sign(formula_profit),
            "final_price": payload.final_price,
            "manual_profit": manual_profit,
            "manual_profit_sign": self._profit_sign(manual_profit),
            "negative_profit": manual_profit is not None and manual_profit < 0,
            "manual_overrides": {"final_price": payload.final_price is not None},
            "warning": rule["source_note"],
        }

    def preview_packaging(self, payload: Any) -> dict[str, Any]:
        product_volume = payload.length_cm * payload.width_cm * payload.height_cm
        carton = self._find_carton(payload.length_cm, payload.width_cm, payload.height_cm)
        if carton is None:
            return {
                "status": "NO_FIT",
                "product_weight_g": payload.product_weight_g,
                "product_volume_cm3": product_volume,
                "predicted_weight_g": None,
                "carton": None,
                "carton_volume_cm3": None,
            }

        carton_view = self._carton_view(carton)
        carton_volume = (
            Decimal(str(carton["length_cm"]))
            * Decimal(str(carton["width_cm"]))
            * Decimal(str(carton["height_cm"]))
        )
        return {
            "status": "PREDICTED",
            "product_weight_g": payload.product_weight_g,
            "product_volume_cm3": product_volume,
            "predicted_weight_g": payload.product_weight_g
            + Decimal(str(carton["recommended_weight_g"])),
            "carton": carton_view,
            "carton_volume_cm3": carton_volume,
        }

    def packaging_options(self) -> list[dict[str, Any]]:
        return [self._carton_view(carton) for carton in self.ordinary_rule["cartons"]]

    def _find_carton(
        self,
        product_length_cm: Decimal,
        product_width_cm: Decimal,
        product_height_cm: Decimal,
    ) -> dict[str, Any] | None:
        matches = []
        for carton in self.ordinary_rule["cartons"]:
            length = Decimal(str(carton["length_cm"]))
            width = Decimal(str(carton["width_cm"]))
            height = Decimal(str(carton["height_cm"]))
            if (
                length >= product_length_cm + self.allowance_cm
                and width >= product_width_cm + self.allowance_cm
                and height >= product_height_cm + self.allowance_cm
            ):
                matches.append((length * width * height, carton))
        return min(matches, key=lambda item: item[0])[1] if matches else None

    @staticmethod
    def _carton_view(carton: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": carton["name"],
            "length_cm": carton["length_cm"],
            "width_cm": carton["width_cm"],
            "height_cm": carton["height_cm"],
            "recommended_weight_g": carton["recommended_weight_g"],
        }

    @staticmethod
    def _profit_sign(profit: Decimal | None) -> str | None:
        if profit is None:
            return None
        if profit > 0:
            return "positive"
        if profit < 0:
            return "negative"
        return "zero"
