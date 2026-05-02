"""Messaging primitive — push-delivery retry columns (Phase 12.1.5 Slice 3b).

Adds two nullable timestamp columns supporting Slice 3b's retry-with-
backoff path:

* ``dispatch_outbox.scheduled_at`` — when this outbox row is eligible
  for dispatch. NULL = dispatch immediately (preserves existing
  behavior for rows not using the retry path). Set on ``retry_message``
  rows to ``now() + backoff_minutes`` to defer dispatch. The
  dispatcher gains ``WHERE dispatched=false AND (scheduled_at IS NULL
  OR scheduled_at <= now())``.

* ``messages.delivering_started_at`` — set when the worker claims a
  message (``queued → delivering`` or ``retry_ready → delivering``).
  Cleared back to NULL on terminal transitions. Used by the new
  stale-recovery poll loop to detect worker-crash-mid-delivery: a
  message stuck in ``delivering`` past
  ``MESSAGE_DELIVERY_STALE_AFTER_SECONDS`` (300s default) gets moved
  back to ``retry_ready`` so the dispatcher can re-enqueue.

Backward-compat contract:

* Both columns nullable + no server default → existing rows fill
  with NULL automatically; no data backfill required.
* ``scheduled_at IS NULL`` matches the existing dispatcher behavior
  (immediate dispatch). Cue-task rows continue to leave it NULL;
  only message-task retry rows populate it.
* ``delivering_started_at IS NULL`` is the steady state for any
  non-claimed message. Stale-recovery only triggers when both
  ``delivery_state = 'delivering'`` AND
  ``delivering_started_at IS NOT NULL`` AND timestamp is past
  threshold.

Indexes:

* Partial index on ``dispatch_outbox(scheduled_at)`` covering
  undispatched rows whose scheduled_at is set — keeps the dispatcher
  query selective for the message-retry case while not affecting the
  cue-task path (those rows have NULL scheduled_at).
* Partial index on ``messages(delivering_started_at)`` covering rows
  in the ``delivering`` state — selective for the stale-recovery
  scan.

Revision ID: 022
Revises: 021
"""
from alembic import op
import sqlalchemy as sa


revision = "022"
down_revision = "021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- 1. dispatch_outbox.scheduled_at -------------------------------
    op.add_column(
        "dispatch_outbox",
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Partial index — only rows pending dispatch with a scheduled_at set.
    # Cue-task rows leave scheduled_at NULL and are not in this index.
    op.create_index(
        "idx_outbox_scheduled",
        "dispatch_outbox",
        ["scheduled_at"],
        postgresql_where=sa.text("dispatched = FALSE AND scheduled_at IS NOT NULL"),
    )

    # ---- 2. messages.delivering_started_at -----------------------------
    op.add_column(
        "messages",
        sa.Column("delivering_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Partial index for the stale-recovery scan: only messages currently
    # in delivering state with a claimed timestamp.
    op.create_index(
        "idx_messages_delivering_started",
        "messages",
        ["delivering_started_at"],
        postgresql_where=sa.text(
            "delivery_state = 'delivering' AND delivering_started_at IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("idx_messages_delivering_started", table_name="messages")
    op.drop_column("messages", "delivering_started_at")
    op.drop_index("idx_outbox_scheduled", table_name="dispatch_outbox")
    op.drop_column("dispatch_outbox", "scheduled_at")
