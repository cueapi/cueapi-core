"""Add alerts table.

Revision ID: 018
Revises: 016

Note: this migration's ``down_revision`` will need to be updated to
``017`` if PR #18 (verification modes, which introduces migration 017)
lands first. Currently chained off 016 so this PR stands alone on
origin/main.

Revises: 016
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "018"
down_revision = "016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "alerts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("cue_id", sa.String(length=20), nullable=True),
        sa.Column("execution_id", UUID(as_uuid=True), nullable=True),
        sa.Column("alert_type", sa.String(length=50), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False, server_default="warning"),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("metadata", JSONB, nullable=True),
        sa.Column("acknowledged", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "alert_type IN ("
            "'outcome_timeout', 'verification_failed', 'consecutive_failures')",
            name="valid_alert_type",
        ),
        sa.CheckConstraint(
            "severity IN ('info', 'warning', 'critical')",
            name="valid_alert_severity",
        ),
    )
    op.create_index("ix_alerts_user_id", "alerts", ["user_id"])
    op.create_index("ix_alerts_user_created", "alerts", ["user_id", "created_at"])
    op.create_index("ix_alerts_execution_id", "alerts", ["execution_id"])


def downgrade() -> None:
    op.drop_index("ix_alerts_execution_id", table_name="alerts")
    op.drop_index("ix_alerts_user_created", table_name="alerts")
    op.drop_index("ix_alerts_user_id", table_name="alerts")
    op.drop_table("alerts")
