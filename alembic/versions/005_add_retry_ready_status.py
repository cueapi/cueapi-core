"""Add retry_ready status to executions

Revision ID: 005
Revises: 004
Create Date: 2026-03-12
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("valid_exec_status", "executions", type_="check")
    op.create_check_constraint(
        "valid_exec_status",
        "executions",
        "status IN ('pending', 'delivering', 'success', 'failed', 'retrying', 'retry_ready')",
    )


def downgrade() -> None:
    op.drop_constraint("valid_exec_status", "executions", type_="check")
    op.create_check_constraint(
        "valid_exec_status",
        "executions",
        "status IN ('pending', 'delivering', 'success', 'failed', 'retrying')",
    )
