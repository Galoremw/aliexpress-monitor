"""Add user roles and forced password changes."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20261028_0017"
down_revision: str | None = "20261021_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(
            sa.Column(
                "role",
                sa.String(length=16),
                nullable=False,
                server_default="operator",
            )
        )
        batch_op.add_column(
            sa.Column(
                "must_change_password",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            )
        )
        batch_op.create_check_constraint(
            "ck_users_role", "role IN ('admin', 'operator')"
        )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_constraint("ck_users_role", type_="check")
        batch_op.drop_column("must_change_password")
        batch_op.drop_column("role")
