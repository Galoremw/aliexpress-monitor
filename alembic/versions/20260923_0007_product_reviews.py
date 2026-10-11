"""Add product roles, public reviews, and review collection tasks.

Revision ID: 20260923_0007
Revises: 20260922_0006
"""

from alembic import op
import sqlalchemy as sa


revision = "20260923_0007"
down_revision = "20260922_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column(
            "business_role",
            sa.String(length=16),
            nullable=False,
            server_default="UNCLASSIFIED",
        ),
    )
    with op.batch_alter_table("products") as batch_op:
        batch_op.create_check_constraint(
            "ck_products_business_role",
            "business_role IN ('SELF', 'COMPETITOR', 'UNCLASSIFIED')",
        )
    op.create_index("ix_products_business_role", "products", ["business_role"])

    op.create_table(
        "product_reviews",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("platform_review_id", sa.String(length=160), nullable=True),
        sa.Column("identity_hash", sa.String(length=64), nullable=False),
        sa.Column("review_text", sa.Text(), nullable=False),
        sa.Column("rating", sa.Numeric(precision=3, scale=2), nullable=True),
        sa.Column("review_date", sa.Date(), nullable=True),
        sa.Column("country_region", sa.String(length=128), nullable=True),
        sa.Column("variant", sa.String(length=512), nullable=True),
        sa.Column("has_media", sa.Boolean(), nullable=True),
        sa.Column("verified_purchase", sa.Boolean(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_data", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("product_id", "identity_hash", name="uq_product_review_identity"),
    )
    op.create_index("ix_product_reviews_product_id", "product_reviews", ["product_id"])
    op.create_index(
        "ix_product_reviews_product_date", "product_reviews", ["product_id", "review_date"]
    )
    op.create_index(
        "ix_product_reviews_product_seen", "product_reviews", ["product_id", "first_seen_at"]
    )

    op.create_table(
        "review_collection_tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("target_url", sa.String(length=2048), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("max_new_reviews", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("scanned_count", sa.Integer(), nullable=False),
        sa.Column("saved_count", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(length=128), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_type", sa.String(length=128), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'PARTIAL', 'FAILED', 'NEEDS_VERIFICATION')",
            name="ck_review_collection_tasks_status",
        ),
        sa.CheckConstraint("max_new_reviews BETWEEN 1 AND 100", name="ck_review_collection_tasks_limit"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_review_collection_tasks_product_id", "review_collection_tasks", ["product_id"])
    op.create_index("ix_review_collection_tasks_status", "review_collection_tasks", ["status"])
    op.create_index(
        "ix_review_collection_tasks_status_created",
        "review_collection_tasks",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_review_collection_tasks_product_created",
        "review_collection_tasks",
        ["product_id", "created_at"],
    )
    op.create_index(
        "uq_review_collection_active_product",
        "review_collection_tasks",
        ["product_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('PENDING', 'RUNNING', 'NEEDS_VERIFICATION')"),
        postgresql_where=sa.text("status IN ('PENDING', 'RUNNING', 'NEEDS_VERIFICATION')"),
    )


def downgrade() -> None:
    op.drop_index("uq_review_collection_active_product", table_name="review_collection_tasks")
    op.drop_index("ix_review_collection_tasks_product_created", table_name="review_collection_tasks")
    op.drop_index("ix_review_collection_tasks_status_created", table_name="review_collection_tasks")
    op.drop_index("ix_review_collection_tasks_status", table_name="review_collection_tasks")
    op.drop_index("ix_review_collection_tasks_product_id", table_name="review_collection_tasks")
    op.drop_table("review_collection_tasks")
    op.drop_index("ix_product_reviews_product_seen", table_name="product_reviews")
    op.drop_index("ix_product_reviews_product_date", table_name="product_reviews")
    op.drop_index("ix_product_reviews_product_id", table_name="product_reviews")
    op.drop_table("product_reviews")
    op.drop_index("ix_products_business_role", table_name="products")
    with op.batch_alter_table("products") as batch_op:
        batch_op.drop_constraint("ck_products_business_role", type_="check")
        batch_op.drop_column("business_role")
