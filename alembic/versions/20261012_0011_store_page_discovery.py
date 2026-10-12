"""Persist the next Orders page for each store's candidate discovery."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20261012_0011"
down_revision: str | None = "20261010_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "store_page_discovery_states",
        sa.Column(
            "store_id",
            sa.Integer(),
            sa.ForeignKey("stores.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("first_orders_url", sa.String(length=2048), nullable=False),
        sa.Column("next_page_url", sa.String(length=2048), nullable=False),
        sa.Column("last_successful_page_url", sa.String(length=2048), nullable=True),
        sa.Column("last_scanned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_outcome", sa.String(length=32), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("store_page_discovery_states")
