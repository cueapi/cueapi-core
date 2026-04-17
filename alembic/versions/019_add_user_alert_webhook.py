"""Add alert_webhook_url + alert_webhook_secret to users.

Revision ID: 019
Revises: 018
"""
from alembic import op
import sqlalchemy as sa

revision = "019"
down_revision = "018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("alert_webhook_url", sa.String(length=2048), nullable=True))
    op.add_column("users", sa.Column("alert_webhook_secret", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "alert_webhook_secret")
    op.drop_column("users", "alert_webhook_url")
