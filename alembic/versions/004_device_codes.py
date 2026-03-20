"""device_codes table

Revision ID: 004
Revises: 003
Create Date: 2025-01-01 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "device_codes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("device_code", sa.String(10), unique=True, nullable=False),
        sa.Column("email", sa.String(255)),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("api_key_plaintext", sa.String(64)),
        sa.Column("verification_token", sa.String(64)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('pending', 'email_sent', 'approved', 'expired', 'claimed')",
            name="valid_dc_status",
        ),
    )
    op.create_index("idx_device_codes_code", "device_codes", ["device_code"])
    op.create_index("idx_device_codes_token", "device_codes", ["verification_token"])


def downgrade() -> None:
    op.drop_table("device_codes")
