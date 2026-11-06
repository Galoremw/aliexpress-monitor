"""Pin Dianxiaomi collection to a dedicated collector PC."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20261104_0020"
down_revision: str | None = "20261101_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "dianxiaomi_queue_control",
        sa.Column("selected_worker_id", sa.String(length=128), nullable=True),
    )
    op.execute(
        """
        UPDATE dianxiaomi_queue_control
        SET selected_worker_id = (
            SELECT worker_id
            FROM browser_collection_workers
            WHERE label LIKE '%第二台电脑%'
               OR COALESCE(display_label_override, '') LIKE '%第二台电脑%'
            ORDER BY last_heartbeat_at DESC, worker_id
            LIMIT 1
        )
        WHERE id = 1
        """
    )


def downgrade() -> None:
    op.drop_column("dianxiaomi_queue_control", "selected_worker_id")
