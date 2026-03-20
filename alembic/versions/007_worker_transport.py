"""Add worker transport support: callback_transport on cues, claim fields on executions, workers table.

Revision ID: 007
Revises: 006
Create Date: 2026-03-12
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade():
    # 1. Add callback_transport to cues with check constraint
    op.add_column("cues", sa.Column("callback_transport", sa.String(10), nullable=False, server_default="webhook"))
    op.create_check_constraint(
        "valid_callback_transport",
        "cues",
        "callback_transport IN ('webhook', 'worker')",
    )

    # 2. Make callback_url nullable (worker cues have no URL)
    op.alter_column("cues", "callback_url", existing_type=sa.Text(), nullable=True)

    # 3. Add claim fields to executions
    op.add_column("executions", sa.Column("claimed_by_worker", sa.String(255), nullable=True))
    op.add_column("executions", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))

    # 4. Create workers table
    op.create_table(
        "workers",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("worker_id", sa.String(255), nullable=False),
        sa.Column("handlers", JSONB, nullable=True),
        sa.Column("last_heartbeat", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "worker_id", name="unique_user_worker"),
    )


def downgrade():
    op.drop_table("workers")
    op.drop_column("executions", "claimed_at")
    op.drop_column("executions", "claimed_by_worker")
    op.drop_constraint("valid_callback_transport", "cues", type_="check")
    op.drop_column("cues", "callback_transport")
    op.alter_column("cues", "callback_url", existing_type=sa.Text(), nullable=False)
