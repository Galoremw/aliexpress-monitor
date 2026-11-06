from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.product_presentation import clean_product_title


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
    initial_collection_status: Literal["queued", "deferred"] | None = None
    initial_collection_message: str | None = None


class ProductCreate(BaseModel):
    store_id: int | None = None
    url: str | None = Field(default=None, max_length=2048)
    aliexpress_product_id: str | None = Field(default=None, max_length=128)
    title: str | None = Field(default=None, max_length=1024)

    @field_validator("title", mode="before")
    @classmethod
    def remove_title_card_metadata(cls, value):
        return clean_product_title(value)

    @model_validator(mode="after")
    def require_url_or_id(self):
        if not self.url and not self.aliexpress_product_id:
            raise ValueError("url or aliexpress_product_id is required")
        return self


class ProductUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=1024)
    status: str | None = Field(default=None, pattern="^(active|inactive)$")
    business_role: Literal["SELF", "COMPETITOR", "UNCLASSIFIED"] | None = None

    @field_validator("title", mode="before")
    @classmethod
    def remove_title_card_metadata(cls, value):
        return clean_product_title(value)


class ProductMonitorClearRequest(BaseModel):
    confirm_clear: Literal[True]


class ProductMonitorClearResponse(BaseModel):
    deactivated_count: int


class ProductRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    store_id: int
    aliexpress_product_id: str
    url: str
    title: str | None
    status: str
    business_role: str
    monitor_scope: str = "competitor"
    discovery_source: str
    discovery_rank: int | None
    discovered_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @field_validator("title", mode="before")
    @classmethod
    def remove_title_card_metadata(cls, value):
        return clean_product_title(value)


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


class HistoricalSalesPoint(BaseModel):
    """A dated sales value explicitly observed from an allowed public source."""

    date: date
    value: int
    value_type: Literal["daily_increment", "cumulative_total"]


class BrowserExtensionCollectionRequest(BaseModel):
    worker_id: str | None = Field(default=None, min_length=1, max_length=128, exclude=True)
    claim_token: str | None = Field(default=None, min_length=1, max_length=128, exclude=True)
    platform_product_id: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=1, max_length=2048)
    title: str | None = Field(default=None, max_length=1024)
    sold_count: int | None = Field(default=None, ge=0)
    price: Decimal | None = Field(default=None, ge=0)
    rating: Decimal | None = Field(default=None, ge=0, le=5)
    review_count: int | None = Field(default=None, ge=0)
    captured_at: datetime | None = None
    historical_sales: list[HistoricalSalesPoint] = Field(default_factory=list, max_length=366)
    raw_data: dict[str, Any] = Field(default_factory=dict)


class BrowserStoreProduct(BaseModel):
    platform_product_id: str = Field(min_length=1, max_length=128)
    orders_rank: int | None = Field(default=None, ge=1, le=50)
    url: str = Field(min_length=1, max_length=2048)
    title: str | None = Field(default=None, max_length=1024)
    observed_product_id: str | None = Field(default=None, max_length=128)
    observed_url: str | None = Field(default=None, max_length=2048)
    observed_page_url: str | None = Field(default=None, max_length=2048)
    identity_match_method: str | None = Field(default=None, max_length=32)
    public_cumulative_sold: int | None = Field(default=None, ge=0)
    image_url: str | None = Field(default=None, max_length=2048)
    historical_sales: list[HistoricalSalesPoint] = Field(default_factory=list, max_length=7)
    history_source: str | None = Field(default=None, max_length=128)
    history_daily_capture_complete: bool = False
    history_expected_dates: list[date] = Field(default_factory=list, max_length=7)
    history_missing_dates: list[date] = Field(default_factory=list, max_length=7)


class BrowserStoreCandidatePage(BaseModel):
    page_url: str = Field(min_length=1, max_length=2048)
    pagination_known: bool = False
    has_next_page: bool
    next_page_url: str | None = Field(default=None, max_length=2048)
    scan_complete: bool
    candidates: list[BrowserStoreProduct] = Field(default_factory=list, max_length=50)
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class BrowserStoreDiscoveryRequest(BaseModel):
    worker_id: str | None = Field(default=None, min_length=1, max_length=128, exclude=True)
    claim_token: str | None = Field(default=None, min_length=1, max_length=128, exclude=True)
    platform_store_id: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=1, max_length=2048)
    products: list[BrowserStoreProduct] = Field(default_factory=list, max_length=50)
    candidate_page: BrowserStoreCandidatePage | None = None
    raw_data: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_products_for_discovery(self) -> "BrowserStoreDiscoveryRequest":
        if not self.products and self.raw_data.get("collection_mode") not in {
            "monitored_store_daily",
            "top_7d_increment_selection",
        }:
            raise ValueError("店铺商品发现结果不能为空")
        return self


class BrowserStoreDiscoveryRead(BaseModel):
    store_id: int
    discovered_count: int
    added_count: int
    existing_count: int
    deactivated_count: int
    skipped_count: int
    product_links: list[str]
    status: str
    error_message: str | None = None
    continuation_saved: bool = False


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


class CollectionProgressItemRead(BaseModel):
    product_id: int
    store_id: int
    store_name: str
    platform_product_id: str
    title: str | None
    product_url: str
    status: str
    status_label: str
    status_class: str
    captured_at: datetime | None


class CollectionProgressRead(BaseModel):
    date: date
    total: int
    completed: int
    pending: int
    failed: int
    items: list[CollectionProgressItemRead]
    next_product: CollectionProgressItemRead | None


class BrowserCollectionRunEnsureRequest(BaseModel):
    target_date: date | None = None
    trigger: str = Field(default="MANUAL", pattern="^(SCHEDULED|MANUAL|CATCH_UP)$")


class BrowserCollectionRunStartRequest(BaseModel):
    helper_worker_ids: list[str] | None = Field(default=None, max_length=32)


class BrowserCollectionRunResumeRequest(BaseModel):
    helper_worker_ids: list[str] | None = Field(default=None, max_length=32)


class BrowserCollectionFailureRequest(BaseModel):
    worker_id: str | None = Field(default=None, min_length=1, max_length=128, exclude=True)
    claim_token: str | None = Field(default=None, min_length=1, max_length=128, exclude=True)
    error_type: str = Field(min_length=1, max_length=128)
    error_message: str = Field(min_length=1, max_length=2000)


class BrowserCollectionChallengeRequest(BaseModel):
    worker_id: str | None = Field(default=None, min_length=1, max_length=128, exclude=True)
    claim_token: str | None = Field(default=None, min_length=1, max_length=128, exclude=True)
    error_message: str | None = Field(default=None, max_length=2000)
    page_url: str | None = Field(default=None, max_length=2048)


class BrowserCollectionIdentityEvidence(BaseModel):
    route_store_id: str = Field(min_length=1, max_length=128, pattern=r"^\d+$")
    heading_store_id: str | None = Field(default=None, max_length=128)
    labeled_store_ids: list[str] = Field(default_factory=list, max_length=32)
    page_title: str | None = Field(default=None, max_length=512)
    ixspy_store_name: str | None = Field(default=None, max_length=255)
    identity_method: str = Field(min_length=1, max_length=64)


class BrowserCollectionIdentityReviewRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)
    claim_token: str = Field(min_length=1, max_length=128)
    page_url: str = Field(min_length=1, max_length=2048)
    identity_error_type: Literal["store_identity_mismatch", "store_identity_unverified"]
    error_message: str = Field(min_length=1, max_length=2000)
    evidence: BrowserCollectionIdentityEvidence


class BrowserCollectionActiveClaim(BaseModel):
    item_id: int
    claim_token: str = Field(min_length=1, max_length=128)


class BrowserCollectionHeartbeatRequest(BaseModel):
    worker_id: str | None = Field(default=None, min_length=1, max_length=128)
    worker_label: str | None = Field(default=None, max_length=128)
    extension_version: str | None = Field(default=None, max_length=64)
    capabilities: list[str] = Field(default_factory=list, max_length=32)
    active_claims: list[BrowserCollectionActiveClaim] = Field(default_factory=list, max_length=2)


class BrowserCollectionClaimRequest(BaseModel):
    worker_id: str | None = Field(default=None, min_length=1, max_length=128)


class BrowserCollectionWorkerSelectRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)
    confirm_handoff: bool = False


class BrowserCollectionWorkerRead(BaseModel):
    worker_id: str
    label: str
    display_label: str
    active_claim_count: int = 0
    extension_version: str | None
    capabilities: list[str] = Field(default_factory=list)
    last_heartbeat_at: datetime
    online: bool


class BrowserCollectionControlRead(BaseModel):
    selected_worker: BrowserCollectionWorkerRead | None
    workers: list[BrowserCollectionWorkerRead]
    dispatch_enabled: bool
    run_status: str | None
    handoff_pending: bool
    draining_count: int
    can_start: bool


class BrowserCollectionMonitoredProductRead(BaseModel):
    platform_product_id: str
    title: str | None
    url: str


class BrowserCollectionItemRead(BaseModel):
    id: int
    run_id: int
    target_key: str
    target_date: date | None = None
    target_type: str
    store_id: int | None
    product_id: int | None
    title: str | None
    target_url: str
    position: int
    status: str
    attempt_count: int
    claimed_at: datetime | None
    completed_at: datetime | None
    error_type: str | None
    error_message: str | None
    identity_review_url: str | None = None
    identity_review_evidence: dict[str, Any] | None = None
    identity_review_pending: bool = False
    identity_review_decision: str | None = None
    monitored_product_ids: list[str] = Field(default_factory=list)
    monitored_products: list[BrowserCollectionMonitoredProductRead] = Field(default_factory=list)
    discovery_page_url: str | None = None
    approved_store_url_identities: list[dict[str, Any]] = Field(default_factory=list)
    collection_start_url: str | None = None


class StorePoolResetCreateRequest(BaseModel):
    confirm_replace: Literal[True]


class StorePoolResetStoreRead(BaseModel):
    id: int
    store_id: int | None
    platform_store_id: str | None
    store_name: str
    orders_url: str
    position: int
    status: str
    scanned_candidate_count: int
    scanned_page_count: int
    next_page_url: str | None
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    error_type: str | None
    error_message: str | None


class StorePoolResetBatchRead(BaseModel):
    id: int
    target_date: date
    window_start: date
    window_end: date
    status: str
    dispatch_enabled: bool
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    stores: list[StorePoolResetStoreRead] = Field(default_factory=list)


class StorePoolResetClaimRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)


class StorePoolResetPageRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)
    claim_token: str = Field(min_length=1, max_length=128)
    expected_store_id: str = Field(min_length=1, max_length=128)
    page: BrowserStoreCandidatePage


class StorePoolResetFailureRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)
    claim_token: str = Field(min_length=1, max_length=128)
    error_type: str = Field(min_length=1, max_length=128)
    error_message: str = Field(min_length=1, max_length=2000)


class StorePoolResetChallengeRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)
    claim_token: str = Field(min_length=1, max_length=128)
    error_message: str | None = Field(default=None, max_length=2000)
    page_url: str | None = Field(default=None, max_length=2048)


class StorePoolResetHeartbeatRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)
    claim_token: str | None = Field(default=None, max_length=128)
    worker_label: str | None = Field(default=None, max_length=128)
    extension_version: str | None = Field(default=None, max_length=64)
    capabilities: list[str] = Field(default_factory=list, max_length=32)


class StorePoolResetResumeRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)
    claim_token: str = Field(min_length=1, max_length=128)
    page_url: str = Field(min_length=1, max_length=2048)


class StorePoolResetClaimRead(BaseModel):
    batch: StorePoolResetBatchRead | None
    store: StorePoolResetStoreRead | None
    claim_token: str | None
    candidate_limit: int = 0
    target_url: str | None
    target_date: date | None


class BrowserCollectionRunRead(BaseModel):
    id: int
    target_date: date
    latest_data_date: date
    primary_worker_id: str | None = None
    helper_worker_ids: list[str] = Field(default_factory=list)
    trigger: str
    status: str
    phase: str
    total_count: int
    succeeded_count: int
    partial_count: int
    failed_count: int
    pending_count: int
    chrome_online: bool
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    last_heartbeat_at: datetime | None
    paused_reason: str | None
    dispatch_enabled: bool
    current_item: BrowserCollectionItemRead | None
    items: list[BrowserCollectionItemRead]


class BrowserCollectionClaimRead(BaseModel):
    run: BrowserCollectionRunRead | None
    item: BrowserCollectionItemRead | None
    claim_token: str | None = None


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
    estimate_type: str = "direct"
    data_basis: str = "ixspy_visible_daily_increment"


class PublicReviewInput(BaseModel):
    platform_review_id: str | None = Field(default=None, max_length=160)
    review_text: str = Field(min_length=1, max_length=12000)
    rating: Decimal | None = Field(default=None, ge=0, le=5)
    review_date: date | None = None
    country_region: str | None = Field(default=None, max_length=128)
    variant: str | None = Field(default=None, max_length=512)
    has_media: bool | None = None
    verified_purchase: bool | None = None
    raw_data: dict[str, Any] = Field(default_factory=dict)


class ReviewCollectionCreate(BaseModel):
    max_new_reviews: int = Field(default=100, ge=1, le=100)


class ReviewCollectionResult(BaseModel):
    status: Literal["SUCCEEDED", "PARTIAL", "FAILED"]
    reviews: list[PublicReviewInput] = Field(default_factory=list, max_length=100)
    scanned_count: int = Field(default=0, ge=0, le=1000)
    error_type: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, max_length=2000)
    raw_data: dict[str, Any] = Field(default_factory=dict)


class ReviewCollectionChallenge(BaseModel):
    error_message: str | None = Field(default=None, max_length=2000)


class ReviewCollectionTaskRead(BaseModel):
    id: int
    product_id: int
    product_title: str | None
    product_url: str
    store_name: str
    status: str
    max_new_reviews: int
    attempt_count: int
    scanned_count: int
    saved_count: int
    created_at: datetime
    claimed_at: datetime | None
    completed_at: datetime | None
    error_type: str | None
    error_message: str | None


class ReviewCollectionClaimRead(BaseModel):
    task: ReviewCollectionTaskRead | None
    known_keys: list[str] = Field(default_factory=list)


class ProductReviewRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    platform_review_id: str | None
    review_text: str
    rating: Decimal | None
    review_date: date | None
    country_region: str | None
    variant: str | None
    has_media: bool | None
    verified_purchase: bool | None
    first_seen_at: datetime


class ReviewInsightsRead(BaseModel):
    total_reviews: int
    rating_distribution: dict[str, int]
    common_terms: list[dict[str, Any]]
    title_terms: list[dict[str, Any]]
    collection_tasks: list[ReviewCollectionTaskRead]


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
    estimate_type: str = "direct_sum"
    estimate_scope: str = "monitored_products_only"
    data_basis: str = "sum_of_ixspy_visible_daily_increments"


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
    deactivated_count: int = 0
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
    estimate_type: str = "direct"
    estimate_scope: str = "discovered_monitored_products"
    data_basis: str = "ixspy_visible_daily_increment"
    products: list[TopProductRead]


class DianxiaomiHandoffCreate(BaseModel):
    product_ids: list[int] = Field(min_length=1, max_length=100)


class DianxiaomiHandoffClaimRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)
    limit: int = Field(default=20, ge=1, le=50)
    resume_confirmed: bool = False


class DianxiaomiQueueHeartbeatRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)


class DianxiaomiWorkerSelectRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=128)


class DianxiaomiHandoffStatusRequest(BaseModel):
    status: str = Field(
        pattern="^(OPENED|FILLED|COLLECTING|SUCCEEDED|FAILED|NEEDS_CONFIRMATION|CANCELED)$"
    )
    worker_id: str | None = Field(default=None, max_length=128)
    error_type: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, max_length=2000)
    detail: dict[str, Any] | None = None


class DianxiaomiHandoffRead(BaseModel):
    id: int
    batch_id: str
    product_id: int
    store_id: int
    product_title: str | None
    platform_product_id: str | None
    store_name: str | None
    target_url: str
    status: str
    requested_at: datetime
    claimed_at: datetime | None
    opened_at: datetime | None
    completed_at: datetime | None
    worker_id: str | None
    error_type: str | None
    error_message: str | None
    attempt_count: int = 0
    detail: dict[str, Any] | None = None


class DianxiaomiHandoffBatchRead(BaseModel):
    batch_id: str
    total_count: int
    queued_count: int
    reused_count: int
    items: list[DianxiaomiHandoffRead]


class SelectionCandidateImportItem(BaseModel):
    platform: Literal["aliexpress"] = "aliexpress"
    aliexpress_product_id: str = Field(min_length=6, max_length=25, pattern=r"^\d+$")
    url: str = Field(min_length=1, max_length=2048)
    title: str | None = Field(default=None, max_length=1024)
    source_candidate_id: str | None = Field(default=None, max_length=128)
    source: str | None = Field(default=None, max_length=128)
    source_metadata: dict[str, Any] = Field(default_factory=dict)


class SelectionCandidatesBatchImport(BaseModel):
    batch_id: str = Field(min_length=1, max_length=128)
    candidates: list[SelectionCandidateImportItem] = Field(min_length=1, max_length=500)


class SelectionQualificationRuleWrite(BaseModel):
    minimum_7d_increment_total: int | None = Field(default=None, ge=0)
    minimum_positive_days: int | None = Field(default=None, ge=0, le=7)


class SelectionQualificationRuleRead(BaseModel):
    minimum_7d_increment_total: int | None
    minimum_positive_days: int | None
    configured: bool
    timezone: str


class DianxiaomiWorkerStatusRequest(BaseModel):
    status: str = Field(
        pattern="^(CLAIMED|OPENED|FILLED|COLLECTING|SUCCEEDED|FAILED|NEEDS_CONFIRMATION)$"
    )
    detail: dict[str, Any] = Field(default_factory=dict)


class DianxiaomiWorkerHandoffRead(BaseModel):
    handoff_id: int
    batch_id: str
    aliexpress_product_id: str
    url: str
    attempt_count: int
    status: str
    detail: dict[str, Any] | None = None
