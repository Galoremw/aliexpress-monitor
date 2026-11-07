"""Add per-run browser worker pause state."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20261105_0021"
down_revision: str | None = "20261104_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "browser_collection_run_paused_workers",
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
    op.drop_table("browser_collection_run_paused_workers")