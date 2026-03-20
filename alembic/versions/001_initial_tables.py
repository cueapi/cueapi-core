"""Initial tables - users and cues

Revision ID: 001
Revises:
Create Date: 2024-01-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

# revision identifiers, used by Alembic.
revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("email", sa.String(255), unique=True, nullable=False),
        sa.Column("api_key_hash", sa.String(64), nullable=False),
        sa.Column("api_key_prefix", sa.String(12), nullable=False),
        sa.Column("plan", sa.String(20), nullable=False, server_default="free"),
        sa.Column("plan_interval", sa.String(10), server_default="monthly"),
        sa.Column("plan_period_end", sa.DateTime(timezone=True)),
        sa.Column("stripe_customer_id", sa.String(64)),
        sa.Column("stripe_subscription_id", sa.String(64)),
        sa.Column("active_cue_limit", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("monthly_execution_limit", sa.Integer(), nullable=False, server_default="300"),
        sa.Column("rate_limit_per_minute", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_users_api_key_hash", "users", ["api_key_hash"], unique=True)
    op.create_index("idx_users_api_key_prefix", "users", ["api_key_prefix"])
    op.create_index("idx_users_email", "users", ["email"])

    op.create_table(
        "cues",
        sa.Column("id", sa.String(20), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("schedule_type", sa.String(10), nullable=False),
        sa.Column("schedule_cron", sa.String(100)),
        sa.Column("schedule_at", sa.DateTime(timezone=True)),
        sa.Column("schedule_timezone", sa.String(50), nullable=False, server_default="UTC"),
        sa.Column("callback_url", sa.Text(), nullable=False),
        sa.Column("callback_method", sa.String(10), nullable=False, server_default="POST"),
        sa.Column("callback_headers", JSONB(), server_default="{}"),
        sa.Column("payload", JSONB(), server_default="{}"),
        sa.Column("retry_max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("retry_backoff_minutes", JSONB(), nullable=False, server_default="[1, 5, 15]"),
        sa.Column("next_run", sa.DateTime(timezone=True)),
        sa.Column("last_run", sa.DateTime(timezone=True)),
        sa.Column("run_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('active', 'paused', 'completed', 'failed')", name="valid_status"),
        sa.CheckConstraint("schedule_type IN ('once', 'recurring')", name="valid_schedule_type"),
        sa.CheckConstraint("callback_method IN ('POST', 'GET', 'PUT', 'PATCH')", name="valid_callback_method"),
    )
    op.create_index("idx_cues_user_id", "cues", ["user_id"])
    op.create_index("idx_cues_status", "cues", ["status"])
    op.create_index(
        "idx_cues_due",
        "cues",
        ["next_run"],
        postgresql_where=sa.text("status = 'active' AND next_run IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_table("cues")
    op.drop_table("users")
