"""Messaging primitive — counterpart-filter composite indexes (v1.1.1).

Supports the new ``?counterpart=<agent_id>`` query param on
``GET /v1/agents/{ref}/inbox`` and ``GET /v1/agents/{ref}/sent``.
Without dedicated composite indexes, the filter would force Postgres
to scan the existing ``ix_messages_inbox(to_agent_id, delivery_state,
created_at)`` and post-filter by ``from_agent_id``. For Dock-shaped
heavy threads (1k+ messages between two participants), that's the
difference between a fast index seek and a 1k-row scan-then-filter.

Two composite indexes (one per direction):

* ``idx_messages_inbox_counterpart`` — covers
  ``WHERE to_agent_id = $self AND from_agent_id = $other`` for the
  inbox-filtered case. Existing ``ix_messages_inbox`` still handles the
  unfiltered inbox poll path; this one only kicks in when the
  ``counterpart`` query param is set.
* ``idx_messages_sent_counterpart`` — covers
  ``WHERE from_agent_id = $self AND to_agent_id = $other`` for the
  symmetric sent-log filter.

Both use ``CREATE INDEX CONCURRENTLY`` because messages is large in
prod (multi-million rows once shared messaging traffic ramps).
Postgres rejects CONCURRENTLY inside a transaction; alembic's
``autocommit_block`` opens a separate connection in autocommit mode.

Trade-off: two extra indexes ≈ +N×24 bytes per message (B-tree
overhead). Acceptable; the read-side improvement on counterpart-filter
queries is 5-100× depending on thread depth, and these indexes also
help any future query that filters on both agents (e.g. thread-
specific dedupe checks, future v1.2.0 group-thread participant
filtering).

Revision ID: 024
Revises: 023
"""
from alembic import op
import sqlalchemy as sa


revision = "024"
down_revision = "023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # CREATE INDEX CONCURRENTLY required because messages is large on
    # prod once shared traffic ramps. Same pattern as 022's partial
    # indexes — autocommit_block lifts each statement out of the
    # surrounding migration transaction so Postgres accepts CONCURRENTLY.
    with op.get_context().autocommit_block():
        op.create_index(
            "idx_messages_inbox_counterpart",
            "messages",
            ["to_agent_id", "from_agent_id", "created_at"],
            postgresql_concurrently=True,
            if_not_exists=True,
        )
        op.create_index(
            "idx_messages_sent_counterpart",
            "messages",
            ["from_agent_id", "to_agent_id", "created_at"],
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "idx_messages_sent_counterpart",
            table_name="messages",
            postgresql_concurrently=True,
            if_exists=True,
        )
        op.drop_index(
            "idx_messages_inbox_counterpart",
            table_name="messages",
            postgresql_concurrently=True,
            if_exists=True,
        )
