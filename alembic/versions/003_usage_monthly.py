"""Add usage_monthly table

Revision ID: 003
Revises: 002
Create Date: 2024-01-03 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "usage_monthly",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("month_start", sa.Date, nullable=False),
        sa.Column("execution_count", sa.Integer, nullable=False, server_default="0"),
        sa.UniqueConstraint("user_id", "month_start", name="unique_user_month"),
    )
    op.create_index("idx_usage_monthly_user", "usage_monthly", ["user_id", "month_start"])


def downgrade():
    op.drop_index("idx_usage_monthly_user")
    op.drop_table("usage_monthly")
