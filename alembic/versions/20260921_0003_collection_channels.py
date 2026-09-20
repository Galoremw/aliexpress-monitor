"""Add channel-neutral snapshots and collection fallback tracking."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260921_0003"
down_revision: str | None = "20260920_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for column in (
        sa.Column("captured_at", sa.DateTime(timezone=True)),
        sa.Column("sold_count", sa.Integer()),
        sa.Column("price", sa.Numeric(18, 4)),
        sa.Column("rating", sa.Numeric(4, 2)),
        sa.Column("source", sa.String(32), server_default="AUTO"),
        sa.Column("status", sa.String(16), server_default="FAILED"),
        sa.Column("raw_data", sa.JSON()),
    ):
        op.add_column("product_snapshots", column)

    snapshots = sa.table(
        "product_snapshots",
        sa.column("captured_at", sa.DateTime()),
        sa.column("collected_at", sa.DateTime()),
        sa.column("sold_count", sa.Integer()),
        sa.column("cumulative_sold", sa.Integer()),
        sa.column("price", sa.Numeric(18, 4)),
        sa.column("price_amount", sa.Numeric(18, 4)),
        sa.column("source", sa.String(32)),
        sa.column("status", sa.String(16)),
        sa.column("parse_status", sa.String(16)),
        sa.column("raw_data", sa.JSON()),
        sa.column("raw_payload", sa.JSON()),
    )
    op.execute(
        snapshots.update().values(
            captured_at=sa.column("collected_at"),
            sold_count=sa.column("cumulative_sold"),
            price=sa.column("price_amount"),
            source="AUTO",
            status=sa.case(
                (sa.column("parse_status") == "success", "VALID"),
                (sa.column("parse_status") == "partial", "SUSPECT"),
                else_="FAILED",
            ),
            raw_data=sa.column("raw_payload"),
        )
    )
    # SQLite cannot ALTER constraints; the ORM table definition still applies
    # to new test databases, while PostgreSQL receives the production checks.
    if op.get_bind().dialect.name != "sqlite":
        op.create_check_constraint(
            "ck_product_snapshots_source",
            "product_snapshots",
            "source IN ('AUTO', 'CHROME_EXTENSION', 'MANUAL', 'API')",
        )
        op.create_check_constraint(
            "ck_product_snapshots_status",
            "product_snapshots",
            "status IN ('VALID', 'SUSPECT', 'FAILED')",
        )
    op.create_index(
        "ix_snapshots_product_captured",
        "product_snapshots",
        ["product_id", "captured_at"],
    )

    op.create_table(
        "collection_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "product_id",
            sa.Integer(),
            sa.ForeignKey("products.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(32), nullable=False, server_default="AUTO"),
        sa.Column("status", sa.String(16), nullable=False, server_default="FAILED"),
        sa.Column("error_type", sa.String(128)),
        sa.Column("error_message", sa.Text()),
        sa.Column(
            "snapshot_id",
            sa.Integer(),
            sa.ForeignKey("product_snapshots.id", ondelete="SET NULL"),
        ),
        sa.CheckConstraint(
            "source IN ('AUTO', 'CHROME_EXTENSION', 'MANUAL', 'API')",
            name="ck_collection_attempts_source",
        ),
        sa.CheckConstraint(
            "status IN ('VALID', 'SUSPECT', 'FAILED')",
            name="ck_collection_attempts_status",
        ),
    )
    op.create_index("ix_collection_attempts_product_id", "collection_attempts", ["product_id"])
    op.create_index(
        "ix_collection_attempts_product_attempted",
        "collection_attempts",
        ["product_id", "attempted_at"],
    )

    op.create_table(
        "manual_collection_tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "product_id",
            sa.Integer(),
            sa.ForeignKey("products.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column(
            "last_attempt_id",
            sa.Integer(),
            sa.ForeignKey("collection_attempts.id", ondelete="SET NULL"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("product_id", name="uq_manual_collection_task_product"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'COMPLETED')",
            name="ck_manual_collection_tasks_status",
        ),
    )
    op.create_index("ix_manual_collection_tasks_status", "manual_collection_tasks", ["status"])


def downgrade() -> None:
    op.drop_table("manual_collection_tasks")
    op.drop_table("collection_attempts")
    op.drop_index("ix_snapshots_product_captured", table_name="product_snapshots")
    if op.get_bind().dialect.name != "sqlite":
        op.drop_constraint("ck_product_snapshots_status", "product_snapshots", type_="check")
        op.drop_constraint("ck_product_snapshots_source", "product_snapshots", type_="check")
    for name in (
        "raw_data",
        "status",
        "source",
        "rating",
        "price",
        "sold_count",
        "captured_at",
    ):
        op.drop_column("product_snapshots", name)
