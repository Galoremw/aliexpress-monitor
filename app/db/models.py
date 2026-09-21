from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (Index("ix_users_username", "username", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(128), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    sessions: Mapped[list[AuthSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    __table_args__ = (
        Index("ix_auth_sessions_token_hash", "token_hash", unique=True),
        Index("ix_auth_sessions_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="sessions")


class Store(Base):
    __tablename__ = "stores"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'inactive')", name="ck_stores_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    aliexpress_store_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(String(2048), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    products: Mapped[list[Product]] = relationship(
        back_populates="store", cascade="all, delete-orphan"
    )
    daily_metrics: Mapped[list[StoreDailyMetric]] = relationship(back_populates="store")
    discovery_snapshots: Mapped[list[StoreSnapshot]] = relationship(
        back_populates="store", cascade="all, delete-orphan"
    )


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'inactive')", name="ck_products_status"),
        UniqueConstraint("store_id", "aliexpress_product_id", name="uq_product_store_external"),
        Index("ix_products_store_status", "store_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"))
    aliexpress_product_id: Mapped[str] = mapped_column(String(128), index=True)
    url: Mapped[str] = mapped_column(String(2048), unique=True)
    title: Mapped[str | None] = mapped_column(String(1024))
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    discovery_source: Mapped[str] = mapped_column(String(32), default="manual")
    discovery_rank: Mapped[int | None] = mapped_column(Integer)
    discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    store: Mapped[Store] = relationship(back_populates="products")
    snapshots: Mapped[list[ProductSnapshot]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )
    daily_metrics: Mapped[list[ProductDailyMetric]] = relationship(back_populates="product")
    collection_attempts: Mapped[list[CollectionAttempt]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )
    manual_collection_tasks: Mapped[list[ManualCollectionTask]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )


class StoreSnapshot(Base):
    __tablename__ = "store_snapshots"
    __table_args__ = (
        CheckConstraint(
            "parse_status IN ('success', 'partial', 'failed')",
            name="ck_store_snapshots_parse_status",
        ),
        Index("ix_store_snapshots_store_collected", "store_id", "collected_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), index=True
    )
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    collector_name: Mapped[str] = mapped_column(String(64))
    collector_version: Mapped[str] = mapped_column(String(32))
    parse_status: Mapped[str] = mapped_column(String(16), index=True)
    candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    source_http_status: Mapped[int | None] = mapped_column(Integer)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    raw_content: Mapped[str | None] = mapped_column(Text)
    error_type: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)

    store: Mapped[Store] = relationship(back_populates="discovery_snapshots")


class ProductSnapshot(Base):
    __tablename__ = "product_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    collector_name: Mapped[str] = mapped_column(String(64))
    collector_version: Mapped[str] = mapped_column(String(32))
    parse_status: Mapped[str] = mapped_column(String(16), index=True)
    cumulative_sold: Mapped[int | None] = mapped_column(Integer)
    price_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    price_currency: Mapped[str | None] = mapped_column(String(8))
    review_count: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str | None] = mapped_column(String(1024))
    source_http_status: Mapped[int | None] = mapped_column(Integer)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    raw_content: Mapped[str | None] = mapped_column(Text)
    error_type: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)

    # Channel-neutral fields. Legacy fields remain for compatibility with the MVP.
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sold_count: Mapped[int | None] = mapped_column(Integer)
    price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    rating: Mapped[Decimal | None] = mapped_column(Numeric(4, 2))
    source: Mapped[str] = mapped_column(String(32), default="AUTO")
    status: Mapped[str] = mapped_column(String(16), default="FAILED")
    raw_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    __table_args__ = (
        CheckConstraint(
            "parse_status IN ('success', 'partial', 'failed')",
            name="ck_product_snapshots_parse_status",
        ),
        CheckConstraint(
            "source IN ('AUTO', 'CHROME_EXTENSION', 'MANUAL', 'API')",
            name="ck_product_snapshots_source",
        ),
        CheckConstraint(
            "status IN ('VALID', 'SUSPECT', 'FAILED')",
            name="ck_product_snapshots_status",
        ),
        Index("ix_snapshots_product_collected", "product_id", "collected_at"),
        Index("ix_snapshots_product_captured", "product_id", "captured_at"),
    )

    product: Mapped[Product] = relationship(back_populates="snapshots")


class CollectionAttempt(Base):
    __tablename__ = "collection_attempts"
    __table_args__ = (
        CheckConstraint(
            "source IN ('AUTO', 'CHROME_EXTENSION', 'MANUAL', 'API')",
            name="ck_collection_attempts_source",
        ),
        CheckConstraint(
            "status IN ('VALID', 'SUSPECT', 'FAILED')",
            name="ck_collection_attempts_status",
        ),
        Index("ix_collection_attempts_product_attempted", "product_id", "attempted_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    source: Mapped[str] = mapped_column(String(32), default="AUTO")
    status: Mapped[str] = mapped_column(String(16), default="FAILED")
    error_type: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("product_snapshots.id", ondelete="SET NULL")
    )

    product: Mapped[Product] = relationship(back_populates="collection_attempts")
    snapshot: Mapped[ProductSnapshot | None] = relationship(foreign_keys=[snapshot_id])


class ManualCollectionTask(Base):
    __tablename__ = "manual_collection_tasks"
    __table_args__ = (
        UniqueConstraint("product_id", name="uq_manual_collection_task_product"),
        CheckConstraint(
            "status IN ('PENDING', 'COMPLETED')",
            name="ck_manual_collection_tasks_status",
        ),
        Index("ix_manual_collection_tasks_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reason: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    last_attempt_id: Mapped[int | None] = mapped_column(
        ForeignKey("collection_attempts.id", ondelete="SET NULL")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    product: Mapped[Product] = relationship(back_populates="manual_collection_tasks")
    last_attempt: Mapped[CollectionAttempt | None] = relationship(foreign_keys=[last_attempt_id])


class BrowserCollectionRun(Base):
    __tablename__ = "browser_collection_runs"
    __table_args__ = (
        UniqueConstraint("target_date", name="uq_browser_collection_run_target_date"),
        CheckConstraint(
            "trigger IN ('SCHEDULED', 'MANUAL', 'CATCH_UP')",
            name="ck_browser_collection_runs_trigger",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'NEEDS_VERIFICATION', 'COMPLETED', 'PARTIAL', 'FAILED')",
            name="ck_browser_collection_runs_status",
        ),
        CheckConstraint(
            "phase IN ('STORE_DISCOVERY', 'PRODUCT_COLLECTION', 'COMPLETED')",
            name="ck_browser_collection_runs_phase",
        ),
        Index("ix_browser_collection_runs_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    target_date: Mapped[date] = mapped_column(Date, nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), default="SCHEDULED")
    status: Mapped[str] = mapped_column(String(32), default="PENDING")
    phase: Mapped[str] = mapped_column(String(32), default="STORE_DISCOVERY")
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    succeeded_count: Mapped[int] = mapped_column(Integer, default=0)
    partial_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paused_reason: Mapped[str | None] = mapped_column(String(128))

    items: Mapped[list[BrowserCollectionItem]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class BrowserCollectionItem(Base):
    __tablename__ = "browser_collection_items"
    __table_args__ = (
        UniqueConstraint("run_id", "target_key", name="uq_browser_collection_item_target"),
        CheckConstraint(
            "target_type IN ('STORE', 'PRODUCT')",
            name="ck_browser_collection_items_target_type",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'PARTIAL', 'FAILED', 'NEEDS_VERIFICATION', 'SKIPPED')",
            name="ck_browser_collection_items_status",
        ),
        Index("ix_browser_collection_items_run_status", "run_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("browser_collection_runs.id", ondelete="CASCADE"), nullable=False
    )
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target_key: Mapped[str] = mapped_column(String(160), nullable=False)
    store_id: Mapped[int | None] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    target_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="PENDING")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_type: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("product_snapshots.id", ondelete="SET NULL")
    )

    run: Mapped[BrowserCollectionRun] = relationship(back_populates="items")
    store: Mapped[Store | None] = relationship()
    product: Mapped[Product | None] = relationship()
    snapshot: Mapped[ProductSnapshot | None] = relationship()


class DianxiaomiHandoff(Base):
    __tablename__ = "dianxiaomi_handoffs"
    __table_args__ = (
        UniqueConstraint("batch_id", "product_id", name="uq_dianxiaomi_handoff_batch_product"),
        CheckConstraint(
            "status IN ('QUEUED', 'CLAIMED', 'OPENED', 'FILLED', 'COLLECTING', 'SUCCEEDED', 'FAILED', 'NEEDS_CONFIRMATION', 'CANCELED')",
            name="ck_dianxiaomi_handoffs_status",
        ),
        Index("ix_dianxiaomi_handoffs_status_requested", "status", "requested_at"),
        Index("ix_dianxiaomi_handoffs_batch", "batch_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    store_id: Mapped[int] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="QUEUED", index=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_id: Mapped[str | None] = mapped_column(String(128))
    error_type: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    product: Mapped[Product] = relationship()
    store: Mapped[Store] = relationship()


class ProductDailyMetric(Base):
    __tablename__ = "product_daily_metrics"
    __table_args__ = (
        UniqueConstraint("product_id", "metric_date", name="uq_product_metric_date"),
        Index("ix_product_metrics_date", "metric_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    metric_date: Mapped[date] = mapped_column(Date)
    estimated_sales: Mapped[int | None] = mapped_column(Integer)
    is_estimable: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(String(128))
    start_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("product_snapshots.id", ondelete="SET NULL")
    )
    end_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("product_snapshots.id", ondelete="SET NULL")
    )
    calculation_method: Mapped[str] = mapped_column(
        String(128), default="public_cumulative_sold_delta"
    )
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    product: Mapped[Product] = relationship(back_populates="daily_metrics")


class StoreDailyMetric(Base):
    __tablename__ = "store_daily_metrics"
    __table_args__ = (
        UniqueConstraint("store_id", "metric_date", name="uq_store_metric_date"),
        Index("ix_store_metrics_date", "metric_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(
        ForeignKey("stores.id", ondelete="CASCADE"), index=True
    )
    metric_date: Mapped[date] = mapped_column(Date)
    estimated_sales: Mapped[int] = mapped_column(Integer, default=0)
    estimable_products: Mapped[int] = mapped_column(Integer, default=0)
    unavailable_products: Mapped[int] = mapped_column(Integer, default=0)
    product_scope_count: Mapped[int] = mapped_column(Integer, default=0)
    calculation_method: Mapped[str] = mapped_column(
        String(128), default="sum_of_monitored_product_estimates"
    )
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    store: Mapped[Store] = relationship(back_populates="daily_metrics")
