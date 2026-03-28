"""Add content_trends table."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        "content_trends",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("scan_date", sa.Date(), nullable=False),
        sa.Column("topics", postgresql.JSONB(), nullable=False),
        sa.Column("backlog_pick", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_content_trends_scan_date", "content_trends", ["scan_date"], unique=True)

def downgrade():
    op.drop_index("ix_content_trends_scan_date", table_name="content_trends")
    op.drop_table("content_trends")
