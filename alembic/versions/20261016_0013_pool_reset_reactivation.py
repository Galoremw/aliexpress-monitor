"""Mark products deactivated by an explicit pool clear as reactivation eligible."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "20261016_0013"
down_revision: str | None = "20261015_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column(
            "pool_reset_reactivation_eligible",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("products", "pool_reset_reactivation_eligible")
