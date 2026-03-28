"""Add image_data column to blog_posts.

Revision ID: 013
Revises: 012
"""
from alembic import op
import sqlalchemy as sa

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("blog_posts", sa.Column("image_data", sa.Text(), nullable=True))

def downgrade() -> None:
    op.drop_column("blog_posts", "image_data")
