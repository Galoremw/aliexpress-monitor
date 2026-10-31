"""Persist per-worker dashboard display labels."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20261031_0018"
down_revision: str | None = "20261028_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("browser_collection_workers") as batch_op:
        batch_op.add_column(sa.Column("display_label_override", sa.String(length=128)))


def downgrade() -> None:
    with op.batch_alter_table("browser_collection_workers") as batch_op:
        batch_op.drop_column("display_label_override")
