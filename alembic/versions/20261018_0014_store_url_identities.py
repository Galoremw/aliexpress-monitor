"""Persist human-approved, store-scoped AliExpress URL identities."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20261018_0014"
down_revision: str | None = "20261016_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "browser_collection_items",
        sa.Column("identity_review_url", sa.String(length=2048), nullable=True),
    )
    op.add_column(
        "browser_collection_items",
        sa.Column("identity_review_evidence", sa.JSON(), nullable=True),
    )
    op.create_table(
        "store_url_identities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "store_id",
            sa.Integer(),
            sa.ForeignKey("stores.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("route_store_id", sa.String(length=128), nullable=False),
        sa.Column("normalized_page_identity", sa.String(length=2048), nullable=False),
        sa.Column("first_observed_url", sa.String(length=2048), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column(
            "source_item_id",
            sa.Integer(),
            sa.ForeignKey("browser_collection_items.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "approved_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.UniqueConstraint(
            "route_store_id", name="uq_store_url_identities_route_store_id"
        ),
        sa.UniqueConstraint(
            "normalized_page_identity",
            name="uq_store_url_identities_normalized_page_identity",
        ),
    )
    op.create_index(
        "ix_store_url_identities_store_id", "store_url_identities", ["store_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_store_url_identities_store_id", table_name="store_url_identities")
    op.drop_table("store_url_identities")
    op.drop_column("browser_collection_items", "identity_review_evidence")
    op.drop_column("browser_collection_items", "identity_review_url")
