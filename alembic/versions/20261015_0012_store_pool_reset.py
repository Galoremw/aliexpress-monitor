"""Persist isolated monitored-store pool reset batches and page checkpoints."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20261015_0012"
down_revision: str | None = "20261012_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "browser_collection_workers",
        sa.Column("capabilities", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.create_table(
        "store_pool_reset_batches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("window_start", sa.Date(), nullable=False),
        sa.Column("window_end", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="PENDING"),
        sa.Column("dispatch_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'NEEDS_VERIFICATION', 'COMPLETED', 'PARTIAL', 'FAILED', 'CANCELLED')",
            name="ck_store_pool_reset_batches_status",
        ),
    )
    op.create_index(
        "ix_store_pool_reset_batches_status_created",
        "store_pool_reset_batches",
        ["status", "created_at"],
    )
    op.create_table(
        "store_pool_reset_stores",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "batch_id",
            sa.Integer(),
            sa.ForeignKey("store_pool_reset_batches.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id", ondelete="SET NULL"), nullable=True),
        sa.Column("platform_store_id", sa.String(length=128), nullable=True),
        sa.Column("store_name", sa.String(length=255), nullable=False),
        sa.Column("orders_url", sa.String(length=2048), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="PENDING"),
        sa.Column("scanned_candidate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("scanned_page_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("page_attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_page_url", sa.String(length=2048), nullable=True),
        sa.Column("visited_page_urls", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("staged_candidates", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("diagnostics", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("claimed_worker_id", sa.String(length=128), nullable=True),
        sa.Column("claim_token", sa.String(length=128), nullable=True),
        sa.Column("claimed_page_url", sa.String(length=2048), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_accepted_claim_token", sa.String(length=128), nullable=True),
        sa.Column("last_accepted_result_digest", sa.String(length=64), nullable=True),
        sa.Column("error_type", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("batch_id", "store_id", name="uq_store_pool_reset_batch_store"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'NEEDS_VERIFICATION', 'SUCCEEDED', 'FAILED', 'SKIPPED', 'CANCELLED')",
            name="ck_store_pool_reset_stores_status",
        ),
    )
    op.create_index(
        "ix_store_pool_reset_stores_batch_status",
        "store_pool_reset_stores",
        ["batch_id", "status"],
    )
    op.create_index(
        "ix_store_pool_reset_stores_lease",
        "store_pool_reset_stores",
        ["status", "lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_store_pool_reset_stores_lease", table_name="store_pool_reset_stores")
    op.drop_index("ix_store_pool_reset_stores_batch_status", table_name="store_pool_reset_stores")
    op.drop_table("store_pool_reset_stores")
    op.drop_index("ix_store_pool_reset_batches_status_created", table_name="store_pool_reset_batches")
    op.drop_table("store_pool_reset_batches")
    op.drop_column("browser_collection_workers", "capabilities")
