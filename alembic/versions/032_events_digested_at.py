"""Phase 4b — `events.digested_at` column for digest batching.

Adds a nullable timestamp column to the ``events`` table. Used by
the digest emitter (Phase 4b) to mark `message.delivered` events of
priority 1 or 2 as "already bundled into a digest" so the next
digest cycle doesn't re-bundle them.

Schema is purely additive. Existing rows get NULL (un-digested) by
default; the digest emitter's first run only acts on un-digested
rows, so historical rows that pre-date the column are silently
skipped (which is correct — we don't want to emit digests of
ancient events at first deploy).

Index on ``digested_at`` (partial, WHERE NULL) supports the
"find un-digested low-priority events for recipient X" query that
the digest emitter runs every period. CONCURRENTLY per the
alembic-collision-guard pattern (PR #69).

Revision ID: 032
Revises: 031
"""
from alembic import op
import sqlalchemy as sa


revision = "032"
down_revision = "031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "events",
        sa.Column("digested_at", sa.DateTime(timezone=True), nullable=True),
    )

    with op.get_context().autocommit_block():
        # Partial index on un-digested rows. The digest emitter's
        # query is "WHERE recipient_agent_id = X AND priority IN (1,2)
        # AND digested_at IS NULL"; this index covers the NULL
        # condition + leaves the priority + recipient filters to the
        # composite index on (recipient_agent_id, id).
        op.create_index(
            "ix_events_undigested",
            "events",
            ["recipient_agent_id"],
            postgresql_where=sa.text("digested_at IS NULL"),
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_events_undigested",
            table_name="events",
            postgresql_concurrently=True,
            if_exists=True,
        )
    op.drop_column("events", "digested_at")
