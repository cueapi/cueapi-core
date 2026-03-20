"""Add on_failure JSONB column to cues table.

Default: {"email": true, "webhook": null, "pause": false}

Revision ID: 016
Revises: 015
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cues",
        sa.Column(
            "on_failure",
            JSONB,
            nullable=True,
            server_default='{"email": true, "webhook": null, "pause": false}',
        ),
    )


def downgrade() -> None:
    op.drop_column("cues", "on_failure")
