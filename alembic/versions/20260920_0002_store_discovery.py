"""Add store discovery snapshots and product provenance."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260920_0002"
down_revision: str | None = "20260920_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column("discovery_source", sa.String(32), nullable=False, server_default="manual"),
    )
    op.add_column("products", sa.Column("discovery_rank", sa.Integer()))
    op.add_column("products", sa.Column("discovered_at", sa.DateTime(timezone=True)))
    op.create_table(
        "store_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "store_id",
            sa.Integer(),
            sa.ForeignKey("stores.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("collector_name", sa.String(64), nullable=False),
        sa.Column("collector_version", sa.String(32), nullable=False),
        sa.Column("parse_status", sa.String(16), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("source_http_status", sa.Integer()),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.Column("raw_content", sa.Text()),
        sa.Column("error_type", sa.String(128)),
        sa.Column("error_message", sa.Text()),
        sa.CheckConstraint(
            "parse_status IN ('success', 'partial', 'failed')",
            name="ck_store_snapshots_parse_status",
        ),
    )
    op.create_index("ix_store_snapshots_store_id", "store_snapshots", ["store_id"])
    op.create_index("ix_store_snapshots_collected_at", "store_snapshots", ["collected_at"])
    op.create_index("ix_store_snapshots_parse_status", "store_snapshots", ["parse_status"])
    op.create_index(
        "ix_store_snapshots_store_collected",
        "store_snapshots",
        ["store_id", "collected_at"],
    )


def downgrade() -> None:
    op.drop_table("store_snapshots")
    op.drop_column("products", "discovered_at")
    op.drop_column("products", "discovery_rank")
    op.drop_column("products", "discovery_source")

