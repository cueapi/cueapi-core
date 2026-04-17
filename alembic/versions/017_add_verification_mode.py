"""Add verification_mode column to cues table.

Outcome-verification policy per cue. Stored as a plain string rather
than JSONB because the only structured field today is `mode`; keeping
it a string keeps queries simple (`WHERE verification_mode = ...`) and
lets Postgres enforce the enum via a CHECK constraint. If the policy
gains fields later, widen to JSONB with a separate migration.

NULL means "no verification" — equivalent to mode=none but avoids a
row rewrite for the 100% of existing rows that have never configured
verification. Outcome service treats NULL and 'none' identically.

Revision ID: 017
Revises: 016
"""
from alembic import op
import sqlalchemy as sa

revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cues",
        sa.Column("verification_mode", sa.String(length=50), nullable=True),
    )
    op.create_check_constraint(
        "valid_verification_mode",
        "cues",
        "verification_mode IS NULL OR verification_mode IN ("
        "'none', 'require_external_id', 'require_result_url', "
        "'require_artifacts', 'manual')",
    )


def downgrade() -> None:
    op.drop_constraint("valid_verification_mode", "cues", type_="check")
    op.drop_column("cues", "verification_mode")
