"""Add webhook_secret to users table and backfill existing rows.

Revision ID: 008
Revises: 007
"""

import secrets

from alembic import op
import sqlalchemy as sa

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def _generate_webhook_secret() -> str:
    return f"whsec_{secrets.token_hex(32)}"


def upgrade():
    # 1. Add webhook_secret column (nullable first for backfill)
    op.add_column(
        "users",
        sa.Column("webhook_secret", sa.String(80), nullable=True),
    )

    # 2. Backfill existing users with unique secrets
    conn = op.get_bind()
    users = conn.execute(sa.text("SELECT id FROM users WHERE webhook_secret IS NULL"))
    for row in users:
        secret = _generate_webhook_secret()
        conn.execute(
            sa.text("UPDATE users SET webhook_secret = :secret WHERE id = :id"),
            {"secret": secret, "id": row[0]},
        )

    # 3. Make column NOT NULL now that all rows are backfilled
    op.alter_column("users", "webhook_secret", nullable=False)


def downgrade():
    op.drop_column("users", "webhook_secret")
