"""Allow manually pausing a browser collection run."""

from collections.abc import Sequence

from alembic import op


revision: str = "20260925_0008"
down_revision: str | None = "20260923_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("browser_collection_runs") as batch_op:
        batch_op.drop_constraint("ck_browser_collection_runs_status", type_="check")
        batch_op.create_check_constraint(
            "ck_browser_collection_runs_status",
            "status IN ('PENDING', 'RUNNING', 'NEEDS_VERIFICATION', 'PAUSED', 'COMPLETED', 'PARTIAL', 'FAILED')",
        )


def downgrade() -> None:
    with op.batch_alter_table("browser_collection_runs") as batch_op:
        batch_op.drop_constraint("ck_browser_collection_runs_status", type_="check")
        batch_op.create_check_constraint(
            "ck_browser_collection_runs_status",
            "status IN ('PENDING', 'RUNNING', 'NEEDS_VERIFICATION', 'COMPLETED', 'PARTIAL', 'FAILED')",
        )
