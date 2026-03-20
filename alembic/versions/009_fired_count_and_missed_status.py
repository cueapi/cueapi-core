"""Add fired_count to cues and missed status to executions

Revision ID: 009
Revises: 008
Create Date: 2026-03-14
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add fired_count column to cues, default 0
    op.add_column("cues", sa.Column("fired_count", sa.Integer(), nullable=False, server_default="0"))

    # Update execution status constraint to include 'missed'
    op.drop_constraint("valid_exec_status", "executions", type_="check")
    op.create_check_constraint(
        "valid_exec_status",
        "executions",
        "status IN ('pending', 'delivering', 'success', 'failed', 'retrying', 'retry_ready', 'missed')",
    )


def downgrade() -> None:
    op.drop_column("cues", "fired_count")

    op.drop_constraint("valid_exec_status", "executions", type_="check")
    op.create_check_constraint(
        "valid_exec_status",
        "executions",
        "status IN ('pending', 'delivering', 'success', 'failed', 'retrying', 'retry_ready')",
    )
