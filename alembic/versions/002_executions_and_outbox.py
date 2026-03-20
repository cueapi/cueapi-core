"""Add executions and dispatch_outbox tables

Revision ID: 002
Revises: 001
Create Date: 2024-01-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

# revision identifiers, used by Alembic.
revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "executions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("cue_id", sa.String(20), sa.ForeignKey("cues.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("http_status", sa.Integer()),
        sa.Column("response_body", sa.Text()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry", sa.DateTime(timezone=True)),
        sa.Column("error_message", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('pending', 'delivering', 'success', 'failed', 'retrying')",
            name="valid_exec_status",
        ),
    )
    op.create_index("idx_executions_dedup", "executions", ["cue_id", "scheduled_for"], unique=True)
    op.create_index("idx_executions_cue_id", "executions", ["cue_id"])
    op.create_index("idx_executions_created_at", "executions", ["created_at"])
    op.create_index(
        "idx_executions_retries",
        "executions",
        ["next_retry"],
        postgresql_where=sa.text("status = 'retrying' AND next_retry IS NOT NULL"),
    )

    op.create_table(
        "dispatch_outbox",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("execution_id", UUID(as_uuid=True), sa.ForeignKey("executions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("cue_id", sa.String(20), nullable=False),
        sa.Column("task_type", sa.String(20), nullable=False, server_default="deliver"),
        sa.Column("payload", JSONB(), nullable=False, server_default="{}"),
        sa.Column("dispatched", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("dispatch_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_dispatch_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("task_type IN ('deliver', 'retry')", name="valid_task_type"),
    )
    op.create_index(
        "idx_outbox_pending",
        "dispatch_outbox",
        ["created_at"],
        postgresql_where=sa.text("dispatched = FALSE"),
    )


def downgrade() -> None:
    op.drop_table("dispatch_outbox")
    op.drop_table("executions")
