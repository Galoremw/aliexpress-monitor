"""Persist queue pause state and local Chrome worker heartbeat."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20260927_0009"
down_revision: str | None = "20260925_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dianxiaomi_queue_control",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "is_paused", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paused_reason", sa.String(length=32), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_id", sa.String(length=128), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.CheckConstraint("id = 1", name="ck_dianxiaomi_queue_control_singleton"),
    )
    op.bulk_insert(
        sa.table(
            "dianxiaomi_queue_control",
            sa.column("id", sa.Integer()),
            sa.column("is_paused", sa.Boolean()),
        ),
        [{"id": 1, "is_paused": False}],
    )


def downgrade() -> None:
    op.drop_table("dianxiaomi_queue_control")
