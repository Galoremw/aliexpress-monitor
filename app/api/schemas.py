from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StoreCreate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    url: str = Field(min_length=1, max_length=2048)
    aliexpress_store_id: str | None = Field(default=None, max_length=128)


class StoreUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    url: str | None = Field(default=None, min_length=1, max_length=2048)
    aliexpress_store_id: str | None = Field(default=None, max_length=128)
    status: str | None = Field(default=None, pattern="^(active|inactive)$")


class StoreRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    aliexpress_store_id: str | None
    name: str
    url: str
    status: str
    created_at: datetime
    updated_at: datetime


class ProductCreate(BaseModel):
    store_id: int
    url: str | None = Field(default=None, max_length=2048)
    aliexpress_product_id: str | None = Field(default=None, max_length=128)
    title: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def require_url_or_id(self):
        if not self.url and not self.aliexpress_product_id:
            raise ValueError("url or aliexpress_product_id is required")
        return self


class ProductUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=1024)
    status: str | None = Field(default=None, pattern="^(active|inactive)$")


class ProductRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    store_id: int
    aliexpress_product_id: str
    url: str
    title: str | None
    status: str
    discovery_source: str
    discovery_rank: int | None
    discovered_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ProductSnapshotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    collected_at: datetime
    collector_name: str
    collector_version: str
    parse_status: str
    cumulative_sold: int | None
    price_amount: Decimal | None
    price_currency: str | None
    review_count: int | None
    title: str | None
    source_http_status: int | None
    raw_payload: dict[str, Any]
    raw_content: str | None
    error_type: str | None
    error_message: str | None
    captured_at: datetime
    sold_count: int | None
    price: Decimal | None
    rating: Decimal | None
    source: str
    status: str
    raw_data: dict[str, Any]


class BrowserExtensionCollectionRequest(BaseModel):
    platform_product_id: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=1, max_length=2048)
    title: str | None = Field(default=None, max_length=1024)
    sold_count: int | None = Field(default=None, ge=0)
    price: Decimal | None = Field(default=None, ge=0)
    rating: Decimal | None = Field(default=None, ge=0, le=5)
    review_count: int | None = Field(default=None, ge=0)
    captured_at: datetime | None = None
    raw_data: dict[str, Any] = Field(default_factory=dict)


class BrowserStoreProduct(BaseModel):
    platform_product_id: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=1, max_length=2048)
    title: str | None = Field(default=None, max_length=1024)
    public_cumulative_sold: int | None = Field(default=None, ge=0)


class BrowserStoreDiscoveryRequest(BaseModel):
    platform_store_id: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=1, max_length=2048)
    products: list[BrowserStoreProduct] = Field(min_length=1, max_length=20)
    raw_data: dict[str, Any] = Field(default_factory=dict)


class BrowserStoreDiscoveryRead(BaseModel):
    store_id: int
    discovered_count: int
    added_count: int
    existing_count: int
    skipped_count: int
    product_links: list[str]
    status: str
    error_message: str | None = None


class ManualCollectionRequest(BrowserExtensionCollectionRequest):
    pass


class CollectionAttemptRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    attempted_at: datetime
    source: str
    status: str
    error_type: str | None
    error_message: str | None
    snapshot_id: int | None


class CollectionPendingRead(BaseModel):
    product_id: int
    platform_product_id: str
    title: str | None
    product_url: str
    store_id: int
    store_name: str
    last_success_at: datetime | None
    failed_at: datetime | None
    failure_reason: str | None


class CollectionStatusRead(BaseModel):
    date: date
    total_products: int
    auto_success: int
    auto_failed: int
    manual_completed: int
    pending_manual: int
    success_rate: float


class ProductDailyMetricRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    metric_date: date
    estimated_sales: int | None
    is_estimable: bool
    reason: str
    start_snapshot_id: int | None
    end_snapshot_id: int | None
    calculation_method: str
    estimate_type: str = "estimated"
    data_basis: str = "public_observable_data"


class StoreDailyMetricRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    store_id: int
    metric_date: date
    estimated_sales: int
    estimable_products: int
    unavailable_products: int
    product_scope_count: int
    calculation_method: str
    estimate_type: str = "estimated"
    estimate_scope: str = "monitored_products_only"
    data_basis: str = "public_observable_data"


class StoreSnapshotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    store_id: int
    collected_at: datetime
    collector_name: str
    collector_version: str
    parse_status: str
    candidate_count: int
    source_http_status: int | None
    error_type: str | None
    error_message: str | None


class StoreDiscoverySummaryRead(BaseModel):
    snapshot_id: int
    parse_status: str
    discovered_count: int
    added_count: int
    product_links: list[str]
    error_type: str | None = None
    error_message: str | None = None


class TopProductRead(BaseModel):
    rank: int
    product_id: int
    aliexpress_product_id: str
    title: str | None
    url: str
    estimated_sales: int


class StoreTopProductsRead(BaseModel):
    store_id: int
    metric_date: date
    status: str
    estimate_type: str = "estimated"
    estimate_scope: str = "discovered_monitored_products"
    data_basis: str = "public_observable_data"
    products: list[TopProductRead]
