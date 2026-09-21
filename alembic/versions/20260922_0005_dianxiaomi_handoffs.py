"""Add Dianxiaomi browser handoff queue."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260922_0005"
down_revision: str | None = "20260921_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dianxiaomi_handoffs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("batch_id", sa.String(64), nullable=False),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("store_id", sa.Integer(), sa.ForeignKey("stores.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_url", sa.String(2048), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="QUEUED"),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("opened_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("worker_id", sa.String(128)),
        sa.Column("error_type", sa.String(128)),
        sa.Column("error_message", sa.Text()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("batch_id", "product_id", name="uq_dianxiaomi_handoff_batch_product"),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'CLAIMED', 'OPENED', 'FILLED', 'COLLECTING', 'SUCCEEDED', 'FAILED', 'NEEDS_CONFIRMATION', 'CANCELED')",
            name="ck_dianxiaomi_handoffs_status",
        ),
    )
    op.create_index("ix_dianxiaomi_handoffs_status_requested", "dianxiaomi_handoffs", ["status", "requested_at"])
    op.create_index("ix_dianxiaomi_handoffs_batch", "dianxiaomi_handoffs", ["batch_id"])
    op.create_index("ix_dianxiaomi_handoffs_store_id", "dianxiaomi_handoffs", ["store_id"])


def downgrade() -> None:
    op.drop_table("dianxiaomi_handoffs")
