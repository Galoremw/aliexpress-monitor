"""Add versioned pricing rules and pricing records."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20261019_0015"
down_revision: Union[str, None] = "20261018_0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.create_table(
        "pricing_rule_versions",
        sa.Column("version", sa.String(length=128), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_sheet", sa.String(length=255), nullable=False),
        sa.Column("rule_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("version"),
    )
    op.create_table(
        "pricing_records",
        sa.Column("record_id", sa.String(length=64), nullable=False),
        sa.Column("product_id", sa.String(length=64), nullable=True),
        sa.Column("product_name", sa.String(length=300), nullable=True),
        sa.Column("sku", sa.String(length=120), nullable=True),
        sa.Column("rule_version", sa.String(length=128), nullable=False),
        sa.Column("input_json", sa.JSON(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("final_price", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'CONFIRMED')", name="ck_pricing_records_status"
        ),
        sa.ForeignKeyConstraint(
            ["rule_version"], ["pricing_rule_versions.version"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("record_id"),
    )
    op.create_index("ix_pricing_records_created_at", "pricing_records", ["created_at"])
    op.create_index("ix_pricing_records_product_id", "pricing_records", ["product_id"])


def downgrade() -> None:
    op.drop_index("ix_pricing_records_product_id", table_name="pricing_records")
    op.drop_index("ix_pricing_records_created_at", table_name="pricing_records")
    op.drop_table("pricing_records")
    op.drop_table("pricing_rule_versions")
