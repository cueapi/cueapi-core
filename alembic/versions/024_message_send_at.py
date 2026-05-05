"""§13 / Phase 12.1.7 — per-message scheduling on POST /v1/messages.

Adds ``messages.send_at`` (TIMESTAMPTZ NULL) so callers can schedule a
message for future delivery in the same shape as the existing per-cue
schedule. Ports cueapi/cueapi#623 to OSS.

Behavior:

* NULL = send now (existing behavior; backward compat for every row
  in the messages table at migration time, plus every future call
  that omits ``send_at``).
* Non-NULL future timestamp = recipient's inbox query gates with
  ``send_at IS NULL OR send_at <= now()`` so the message is invisible
  until its time. Push-delivery dispatch already gates on
  ``DispatchOutbox.scheduled_at`` from migration 022 — service layer
  plumbs send_at into that column so the dispatcher delays the push
  too.
* Past timestamps are forgiving fallback ("send now"); enforced at the
  service layer, not the column.

Index covers the inbox-fetch hot path: per-recipient messages
filtered by send_at <= now(). Partial index on rows where
send_at IS NOT NULL — the common case (NULL) doesn't need this index
since the existing per-recipient inbox index already covers that path.

Revision ID: 024
Revises: 023
"""
from alembic import op
import sqlalchemy as sa


revision = "024"
down_revision = "023"


def upgrade():
    op.add_column(
        "messages",
        sa.Column("send_at", sa.DateTime(timezone=True), nullable=True),
    )
    # CREATE INDEX CONCURRENTLY: avoids ACCESS EXCLUSIVE lock on a
    # potentially large messages table. Postgres rejects CONCURRENTLY
    # inside a transaction; alembic's autocommit_block opens a separate
    # connection in autocommit mode.
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
