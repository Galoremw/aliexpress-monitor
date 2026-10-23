from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class ProductType(str, Enum):
    ORDINARY = "ordinary"
    PRINTED_3D = "printed_3d"


class PrintedMaterial(str, Enum):
    PLA = "PLA"
    PETG = "PETG"


class PricingQuoteInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    product_id: str | None = Field(default=None, max_length=64)
    product_name: str = Field(default="", max_length=300)
    sku: str = Field(default="", max_length=120)
    product_type: ProductType = ProductType.ORDINARY
    purchase_price: Decimal | None = Field(default=None, ge=0)
    packaging_fee: Decimal | None = Field(default=None, ge=0)
    multiplier: Decimal | None = Field(default=None, gt=0)
    material: PrintedMaterial | None = None
    filament_grams: Decimal | None = Field(default=None, gt=0)
    print_hours: int | None = Field(default=None, ge=0)
    print_minutes: int | None = Field(default=None, ge=0, le=59)
    final_price: Decimal | None = Field(default=None, ge=0)


class PackagingPreviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    product_weight_g: Decimal = Field(gt=0)
    length_cm: Decimal = Field(gt=0)
    width_cm: Decimal = Field(gt=0)
    height_cm: Decimal = Field(gt=0)
