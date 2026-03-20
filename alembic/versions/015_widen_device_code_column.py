"""Widen device_code column from varchar(10) to varchar(128).

Revision ID: 015
Revises: 014
"""
from alembic import op
import sqlalchemy as sa

revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "device_codes",
        "device_code",
        type_=sa.String(128),
        existing_type=sa.String(10),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "device_codes",
        "device_code",
        type_=sa.String(10),
        existing_type=sa.String(128),
        existing_nullable=False,
    )
