"""§13 / Phase 12.1.7 — per-message scheduling on POST /v1/messages.

Adds ``messages.send_at`` (TIMESTAMPTZ NULL) so callers can schedule a
message for future delivery in the same shape as the existing per-cue
schedule. Mirrors PR #618's cue-fire ``send_at`` semantics on the
messaging primitive.

Behavior:

* NULL = send now (existing behavior; backward compat for every row
  in the messages table at migration time, plus every future call
  that omits ``send_at``).
* Non-NULL future timestamp = recipient's inbox query gates with
  ``send_at IS NULL OR send_at <= now()`` so the message is invisible
  until its time. Push-delivery dispatch already gates on
  ``DispatchOutbox.scheduled_at`` from Slice 3b — service layer plumbs
  send_at into that column so the dispatcher delays the push too.
* Past timestamps are forgiving fallback ("send now"); enforced at the
  service layer, not the column.

Index covers the inbox-fetch hot path: per-recipient messages
filtered by send_at <= now(). Combined with the existing
``ix_messages_inbox(to_agent_id, delivery_state, created_at)``, this
adds a partial index on rows where send_at IS NOT NULL — the
common case (NULL) doesn't need this index since the existing inbox
index already excludes via the ``send_at IS NULL OR send_at <= now()``
predicate when send_at is NULL.

Revision ID: 030
Revises: 029
"""
from alembic import op
import sqlalchemy as sa


revision = "030"
down_revision = "029"


def upgrade():
    op.add_column(
        "messages",
        sa.Column("send_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Partial index: only rows where send_at IS NOT NULL. The hot path is
    # "is the future-scheduled message past its send time yet?" — for
    # rows with NULL send_at (the dominant case), the existing
    # ix_messages_inbox already covers per-recipient lookups.
    #
    # CREATE INDEX CONCURRENTLY required because messages is large on
    # prod (multi-million rows once shared messaging gets traffic).
    # Postgres rejects CONCURRENTLY inside a transaction; alembic's
    # autocommit_block opens a separate connection in autocommit mode.
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_messages_send_at",
            "messages",
            ["send_at"],
            postgresql_where=sa.text("send_at IS NOT NULL"),
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade():
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_messages_send_at",
            table_name="messages",
            postgresql_concurrently=True,
            if_exists=True,
        )
    op.drop_column("messages", "send_at")
