"""Add outcome columns to executions table.

Revision ID: 006
Revises: 005
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("executions", sa.Column("outcome_success", sa.Boolean(), nullable=True))
    op.add_column("executions", sa.Column("outcome_result", sa.Text(), nullable=True))
    op.add_column("executions", sa.Column("outcome_error", sa.Text(), nullable=True))
    op.add_column("executions", sa.Column("outcome_metadata", JSONB(), nullable=True))
    op.add_column("executions", sa.Column("outcome_recorded_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("executions", "outcome_recorded_at")
    op.drop_column("executions", "outcome_metadata")
    op.drop_column("executions", "outcome_error")
    op.drop_column("executions", "outcome_result")
    op.drop_column("executions", "outcome_success")
