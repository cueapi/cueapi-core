"""Add blog_posts table

Revision ID: 011
Revises: 010
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from alembic import op

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        "blog_posts",
        sa.Column("id", UUID(as_uuid=True), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("seo_title", sa.Text()),
        sa.Column("seo_description", sa.Text()),
        sa.Column("category", sa.Text()),
        sa.Column("author", sa.Text(), server_default="Govind Kavaturi"),
        sa.Column("date", sa.Date()),
        sa.Column("read_time", sa.Text()),
        sa.Column("image_url", sa.Text()),
        sa.Column("image_alt", sa.Text()),
        sa.Column("tags", ARRAY(sa.Text())),
        sa.Column("keywords", ARRAY(sa.Text())),
        sa.Column("published", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="unique_blog_slug"),
        sa.CheckConstraint("category IS NULL OR category IN ('tutorial', 'guide', 'blog')", name="valid_blog_category"),
    )
    op.create_index("ix_blog_posts_slug", "blog_posts", ["slug"])
    op.create_index("ix_blog_posts_published", "blog_posts", ["published"])

def downgrade():
    op.drop_index("ix_blog_posts_published")
    op.drop_index("ix_blog_posts_slug")
    op.drop_table("blog_posts")
