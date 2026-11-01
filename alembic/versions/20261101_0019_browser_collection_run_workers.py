"""Persist primary and helper workers for each browser collection run."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20261101_0019"
down_revision: str | None = "20261031_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("browser_collection_runs") as batch_op:
        batch_op.add_column(sa.Column("primary_worker_id", sa.String(length=128), nullable=True))
        batch_op.create_foreign_key(
            "fk_browser_collection_runs_primary_worker_id",
            "browser_collection_workers",
            ["primary_worker_id"],
            ["worker_id"],
        )

    op.create_table(
        "browser_collection_run_helpers",
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(length=128), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["browser_collection_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["worker_id"], ["browser_collection_workers.worker_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("run_id", "worker_id"),
    )


def downgrade() -> None:
    op.drop_table("browser_collection_run_helpers")
    with op.batch_alter_table("browser_collection_runs") as batch_op:
        batch_op.drop_constraint(
            "fk_browser_collection_runs_primary_worker_id", type_="foreignkey"
        )
        batch_op.drop_column("primary_worker_id")
