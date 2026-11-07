"""Require explicit start for each Dianxiaomi queue run."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20261106_0022"
down_revision: str | None = "20261105_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "dianxiaomi_queue_control",
        sa.Column("active_run_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        """
        UPDATE dianxiaomi_queue_control
        SET active_run_started_at = NOW()
        WHERE id = 1
          AND is_paused = FALSE
          AND EXISTS (
              SELECT 1 FROM dianxiaomi_handoffs
              WHERE status IN ('CLAIMED', 'OPENED', 'FILLED', 'COLLECTING')
          )
        """
    )
    op.execute(
        """
        UPDATE dianxiaomi_queue_control
        SET is_paused = TRUE,
            paused_at = COALESCE(paused_at, NOW()),
            paused_reason = 'awaiting_start',
            active_run_started_at = NULL
        WHERE id = 1 AND paused_reason = 'needs_confirmation'
        """
    )
    op.execute(
        """
        UPDATE dianxiaomi_queue_control
        SET is_paused = TRUE,
            paused_at = NOW(),
            paused_reason = 'awaiting_start',
            active_run_started_at = NULL
        WHERE id = 1
          AND is_paused = FALSE
          AND EXISTS (
              SELECT 1 FROM dianxiaomi_handoffs WHERE status = 'QUEUED'
          )
          AND NOT EXISTS (
              SELECT 1 FROM dianxiaomi_handoffs
              WHERE status IN ('CLAIMED', 'OPENED', 'FILLED', 'COLLECTING')
          )
        """
    )


def downgrade() -> None:
    op.drop_column("dianxiaomi_queue_control", "active_run_started_at")