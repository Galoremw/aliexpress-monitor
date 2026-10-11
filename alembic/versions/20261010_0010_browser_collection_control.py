"""Add held dispatch and exclusive browser-worker ownership."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20261010_0010"
down_revision: str | None = "20260927_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "browser_collection_controls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("selected_worker_id", sa.String(length=128), nullable=True),
        sa.Column("selected_worker_label", sa.String(length=128), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.CheckConstraint("id = 1", name="ck_browser_collection_controls_singleton"),
    )
    op.create_table(
        "browser_collection_workers",
        sa.Column("worker_id", sa.String(length=128), primary_key=True),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("extension_version", sa.String(length=64), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.bulk_insert(
        sa.table(
            "browser_collection_controls",
            sa.column("id", sa.Integer()),
            sa.column("selected_worker_id", sa.String()),
            sa.column("selected_worker_label", sa.String()),
        ),
        [{"id": 1, "selected_worker_id": None, "selected_worker_label": None}],
    )
    op.add_column(
        "browser_collection_runs",
        sa.Column(
            "dispatch_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "browser_collection_items",
        sa.Column("claimed_worker_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "browser_collection_items",
        sa.Column("claim_token", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("browser_collection_items", "claim_token")
    op.drop_column("browser_collection_items", "claimed_worker_id")
    op.drop_column("browser_collection_runs", "dispatch_enabled")
    op.drop_table("browser_collection_workers")
    op.drop_table("browser_collection_controls")
