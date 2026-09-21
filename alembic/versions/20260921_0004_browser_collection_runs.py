"""Add durable browser collection runs and queue items."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260921_0004"
down_revision: str | None = "20260921_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "browser_collection_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("trigger", sa.String(16), nullable=False, server_default="SCHEDULED"),
        sa.Column("status", sa.String(32), nullable=False, server_default="PENDING"),
        sa.Column("phase", sa.String(32), nullable=False, server_default="STORE_DISCOVERY"),
        sa.Column("total_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("succeeded_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("partial_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("paused_reason", sa.String(128)),
        sa.UniqueConstraint("target_date", name="uq_browser_collection_run_target_date"),
        sa.CheckConstraint(
            "trigger IN ('SCHEDULED', 'MANUAL', 'CATCH_UP')",
            name="ck_browser_collection_runs_trigger",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'NEEDS_VERIFICATION', 'COMPLETED', 'PARTIAL', 'FAILED')",
            name="ck_browser_collection_runs_status",
        ),
        sa.CheckConstraint(
            "phase IN ('STORE_DISCOVERY', 'PRODUCT_COLLECTION', 'COMPLETED')",
            name="ck_browser_collection_runs_phase",
        ),
    )
    op.create_index(
        "ix_browser_collection_runs_status", "browser_collection_runs", ["status"]
    )

    op.create_table(
        "browser_collection_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("browser_collection_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("target_type", sa.String(16), nullable=False),
        sa.Column("target_key", sa.String(160), nullable=False),
        sa.Column(
            "store_id",
            sa.Integer(),
            sa.ForeignKey("stores.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "product_id",
            sa.Integer(),
            sa.ForeignKey("products.id", ondelete="CASCADE"),
        ),
        sa.Column("target_url", sa.String(2048), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="PENDING"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("error_type", sa.String(128)),
        sa.Column("error_message", sa.Text()),
        sa.Column(
            "snapshot_id",
            sa.Integer(),
            sa.ForeignKey("product_snapshots.id", ondelete="SET NULL"),
        ),
        sa.UniqueConstraint("run_id", "target_key", name="uq_browser_collection_item_target"),
        sa.CheckConstraint(
            "target_type IN ('STORE', 'PRODUCT')",
            name="ck_browser_collection_items_target_type",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'PARTIAL', 'FAILED', 'NEEDS_VERIFICATION', 'SKIPPED')",
            name="ck_browser_collection_items_status",
        ),
    )
    op.create_index(
        "ix_browser_collection_items_run_status",
        "browser_collection_items",
        ["run_id", "status"],
    )
    op.create_index(
        "ix_browser_collection_items_store_id", "browser_collection_items", ["store_id"]
    )
    op.create_index(
        "ix_browser_collection_items_product_id", "browser_collection_items", ["product_id"]
    )


def downgrade() -> None:
    op.drop_table("browser_collection_items")
    op.drop_table("browser_collection_runs")
