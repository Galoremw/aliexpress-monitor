"""Add selection candidate qualification and worker handoff details."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20261021_0016"
down_revision: str | None = "20261019_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("products") as batch_op:
        batch_op.add_column(
            sa.Column(
                "monitor_scope",
                sa.String(length=32),
                nullable=False,
                server_default="competitor",
            )
        )
        batch_op.create_check_constraint(
            "ck_products_monitor_scope",
            "monitor_scope IN ('competitor', 'selection_candidate')",
        )
        batch_op.create_index("ix_products_monitor_scope", ["monitor_scope"])

    with op.batch_alter_table("dianxiaomi_handoffs") as batch_op:
        batch_op.add_column(
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("detail_json", sa.JSON(), nullable=True))

    op.create_table(
        "selection_qualification_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("minimum_7d_increment_total", sa.Integer(), nullable=True),
        sa.Column("minimum_positive_days", sa.Integer(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.CheckConstraint("id = 1", name="ck_selection_qualification_rules_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "selection_candidates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column(
            "qualification_status",
            sa.String(length=32),
            nullable=False,
            server_default="RULE_UNCONFIGURED",
        ),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("handoff_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.ForeignKeyConstraint(["handoff_id"], ["dianxiaomi_handoffs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("handoff_id", name="uq_selection_candidates_handoff_id"),
        sa.UniqueConstraint("product_id", name="uq_selection_candidates_product_id"),
    )
    op.create_table(
        "selection_candidate_imports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("candidate_id", sa.Integer(), nullable=False),
        sa.Column("batch_id", sa.String(length=128), nullable=False),
        sa.Column("source_candidate_id", sa.String(length=128), nullable=True),
        sa.Column("submitted_url", sa.String(length=2048), nullable=False),
        sa.Column("source_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["selection_candidates.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("candidate_id", "batch_id", name="uq_selection_candidate_batch"),
    )
    op.create_index(
        "ix_selection_candidate_imports_batch",
        "selection_candidate_imports",
        ["batch_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_selection_candidate_imports_batch", table_name="selection_candidate_imports"
    )
    op.drop_table("selection_candidate_imports")
    op.drop_table("selection_candidates")
    op.drop_table("selection_qualification_rules")
    with op.batch_alter_table("dianxiaomi_handoffs") as batch_op:
        batch_op.drop_column("detail_json")
        batch_op.drop_column("attempt_count")
    with op.batch_alter_table("products") as batch_op:
        batch_op.drop_index("ix_products_monitor_scope")
        batch_op.drop_constraint("ck_products_monitor_scope", type_="check")
        batch_op.drop_column("monitor_scope")
