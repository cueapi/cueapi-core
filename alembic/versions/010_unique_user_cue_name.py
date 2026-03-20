"""Add unique constraint on (user_id, name) for cues table

Revision ID: 010
Revises: 009
Create Date: 2026-03-14
"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint("unique_user_cue_name", "cues", ["user_id", "name"])


def downgrade():
    op.drop_constraint("unique_user_cue_name", "cues", type_="unique")
