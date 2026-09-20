"""Initial monitoring schema."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260920_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stores",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("aliexpress_store_id", sa.String(128), unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('active', 'inactive')", name="ck_stores_status"),
    )
    op.create_index("ix_stores_status", "stores", ["status"])
    op.create_table(
        "products",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id", ondelete="CASCADE"), nullable=False),
        sa.Column("aliexpress_product_id", sa.String(128), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False, unique=True),
        sa.Column("title", sa.String(1024)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('active', 'inactive')", name="ck_products_status"),
        sa.UniqueConstraint("store_id", "aliexpress_product_id", name="uq_product_store_external"),
    )
    op.create_index("ix_products_aliexpress_product_id", "products", ["aliexpress_product_id"])
    op.create_index("ix_products_status", "products", ["status"])
    op.create_index("ix_products_store_status", "products", ["store_id", "status"])
    op.create_table(
        "product_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("collector_name", sa.String(64), nullable=False),
        sa.Column("collector_version", sa.String(32), nullable=False),
        sa.Column("parse_status", sa.String(16), nullable=False),
        sa.Column("cumulative_sold", sa.Integer()),
        sa.Column("price_amount", sa.Numeric(18, 4)),
        sa.Column("price_currency", sa.String(8)),
        sa.Column("review_count", sa.Integer()),
        sa.Column("title", sa.String(1024)),
        sa.Column("source_http_status", sa.Integer()),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.Column("raw_content", sa.Text()),
        sa.Column("error_type", sa.String(128)),
        sa.Column("error_message", sa.Text()),
        sa.CheckConstraint("parse_status IN ('success', 'partial', 'failed')", name="ck_product_snapshots_parse_status"),
    )
    op.create_index("ix_product_snapshots_collected_at", "product_snapshots", ["collected_at"])
    op.create_index("ix_product_snapshots_parse_status", "product_snapshots", ["parse_status"])
    op.create_index("ix_product_snapshots_product_id", "product_snapshots", ["product_id"])
    op.create_index("ix_snapshots_product_collected", "product_snapshots", ["product_id", "collected_at"])
    op.create_table(
        "product_daily_metrics",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("metric_date", sa.Date(), nullable=False),
        sa.Column("estimated_sales", sa.Integer()),
        sa.Column("is_estimable", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(128), nullable=False),
        sa.Column("start_snapshot_id", sa.Integer(), sa.ForeignKey("product_snapshots.id", ondelete="SET NULL")),
        sa.Column("end_snapshot_id", sa.Integer(), sa.ForeignKey("product_snapshots.id", ondelete="SET NULL")),
        sa.Column("calculation_method", sa.String(128), nullable=False),
        sa.Column("calculated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("product_id", "metric_date", name="uq_product_metric_date"),
    )
    op.create_index("ix_product_daily_metrics_product_id", "product_daily_metrics", ["product_id"])
    op.create_index("ix_product_metrics_date", "product_daily_metrics", ["metric_date"])
    op.create_table(
        "store_daily_metrics",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id", ondelete="CASCADE"), nullable=False),
        sa.Column("metric_date", sa.Date(), nullable=False),
        sa.Column("estimated_sales", sa.Integer(), nullable=False),
        sa.Column("estimable_products", sa.Integer(), nullable=False),
        sa.Column("unavailable_products", sa.Integer(), nullable=False),
        sa.Column("product_scope_count", sa.Integer(), nullable=False),
        sa.Column("calculation_method", sa.String(128), nullable=False),
        sa.Column("calculated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("store_id", "metric_date", name="uq_store_metric_date"),
    )
    op.create_index("ix_store_daily_metrics_store_id", "store_daily_metrics", ["store_id"])
    op.create_index("ix_store_metrics_date", "store_daily_metrics", ["metric_date"])


def downgrade() -> None:
    op.drop_table("store_daily_metrics")
    op.drop_table("product_daily_metrics")
    op.drop_table("product_snapshots")
    op.drop_table("products")
    op.drop_table("stores")

