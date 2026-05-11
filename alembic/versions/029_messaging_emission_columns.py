"""Messaging emission columns (PR-2a).

Adds 3 columns to the ``messages`` table to support the event-emit
primitive wiring per the Cue Messages design lock:

* ``dispatch_priority_bucket`` (smallint, default 3) — computed at
  message-create time from ``priority``. Lets the server-side
  dispatcher rate-limit / batch by tier without re-querying every
  consumer. v0.1 uses priority verbatim; future per-tier policy
  (Q3 design lock) reads this column instead of ``priority`` so
  the bucket can diverge from raw priority over time.
* ``message_dispatch_error`` (text, nullable) — captures handler
  error context per CC-cueapi's EC1 in the design appendix:
  "handler returned error but reported outcome_success" — pipe
  outcome.error into this column so the sender's audit trail
  shows both successful-delivery + downstream-handler-error.
* ``correlation_id`` (varchar 255, nullable) — RPC framing per
  D3. Sender-supplied opaque string for programmatic
  request/response matching independent of reply_to chains.

All three are purely additive; existing rows get default values
(dispatch_priority_bucket=3 via server_default; others NULL).
Backward-compat: existing senders that don't supply
``correlation_id`` get NULL; existing handler errors stay opaque
(message_dispatch_error stays NULL until a future commit wires
the outcome→message bridge).

Index on ``correlation_id`` (partial, WHERE NOT NULL) supports
the future RPC-match lookup pattern. CONCURRENTLY per the
alembic-collision-guard pattern (PR #69).

Revision ID: 029
Revises: 028
"""
from alembic import op
import sqlalchemy as sa


revision = "029"
down_revision = "028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column(
            "dispatch_priority_bucket",
            sa.SmallInteger,
            nullable=False,
            server_default="3",
        ),
    )
    op.add_column(
        "messages",
        sa.Column("message_dispatch_error", sa.Text, nullable=True),
    )
    op.add_column(
        "messages",
        sa.Column("correlation_id", sa.String(255), nullable=True),
    )

    with op.get_context().autocommit_block():
        op.create_index(
            "ix_messages_correlation_id",
            "messages",
            ["correlation_id"],
            postgresql_where=sa.text("correlation_id IS NOT NULL"),
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_messages_correlation_id",
            table_name="messages",
            postgresql_concurrently=True,
            if_exists=True,
        )
    op.drop_column("messages", "correlation_id")
    op.drop_column("messages", "message_dispatch_error")
    op.drop_column("messages", "dispatch_priority_bucket")
