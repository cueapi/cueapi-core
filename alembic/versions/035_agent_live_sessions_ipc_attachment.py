"""Item B Phase 1 — extend agent_live_sessions for live-delivery-v3 IPC attachments.

Adds the substrate-side primitives needed for the daemon-IPC delivery path
locked in the live-delivery-v3 joint design at:

    https://trydock.ai/mike/live-delivery-v3-build-hub

Schema additions (4 columns + 2 indexes; all nullable / defaulted so existing
v2.x rows continue to work unchanged):

* ``ipc_session_token VARCHAR(32)`` — daemon-issued ULID identifying the
  attachment for fire-accept routing. NULL for non-IPC rows. VARCHAR(32) per
  primary's Q-A REDIRECT (room for future versioned-prefix shapes like
  ``v3a_<26-char-ULID>``); application-layer validates format (no DB regex
  CHECK per CueAPI convention).
* ``transport VARCHAR(8) NOT NULL DEFAULT 'poll'`` + CHECK IN ('ipc', 'poll')
  — routing-mode for fire-accept dispatcher. Existing rows default to 'poll'
  so behavior is unchanged until the daemon issues a v3 attach. VARCHAR+CHECK
  matches CueAPI convention (cues.callback_transport, executions.status,
  users.plan all use this shape; Postgres ENUM ALTER is painful to evolve).
* ``daemon_id UUID`` — stable per-install Desktop daemon identity, sent in
  ``X-CueAPI-Daemon-Id`` header on attach + reconcile + DELETE requests.
  Server scopes reconcile transactions per-daemon so daemon X's view can't
  affect daemon Y's rows. NULL for v2.x rows (no daemon identity tracked).
* ``last_reconciled_at TIMESTAMPTZ`` — bumps every time a row appears in a
  daemon reconcile batch. Powers the conservative downgrade-to-poll cleanup
  for orphaned rows (rows in DB not mentioned in current reconcile batch get
  ``transport='poll'`` and ``last_reconciled_at`` left untouched; daily
  cleanup deletes ``transport='poll'`` rows where ``last_reconciled_at <
  now() - 24h``).

Indexes:

* ``ix_agent_live_sessions_daemon`` on ``daemon_id`` — supports per-daemon
  reconcile WHERE clauses.
* ``ix_agent_live_sessions_transport`` on ``(transport, last_reconciled_at)``
  — supports the daily cleanup job's filter ``transport='poll' AND
  last_reconciled_at < now() - 24h``.

Mike Q-B ratification 2026-05-12 ~00:38Z locked the **ASYNC** fire-accept
dispatcher path: server fires + returns immediately with
``delivery_mode_requested='ipc'``; daemon-side delivery ack happens via the
existing ``POST /v1/executions/<id>/outcome`` path. NO inline ack-callback
machinery in Phase 1. Sync-inline-ack-3s alternative deferred to a future
Backlog row (meeting-room-style live agent discussions).

Backwards-compat: all v2.x rows inherit ``transport='poll'`` + NULL on the
other 3 columns. Existing fire-accept dispatcher logic untouched until a row
carries ``transport='ipc'``.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID


revision = "035"
down_revision = "034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_live_sessions",
        sa.Column("ipc_session_token", sa.String(32), nullable=True),
    )
    op.add_column(
        "agent_live_sessions",
        sa.Column(
            "transport",
            sa.String(8),
            nullable=False,
            server_default=sa.text("'poll'"),
        ),
    )
    op.add_column(
        "agent_live_sessions",
        sa.Column("daemon_id", UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "agent_live_sessions",
        sa.Column(
            "last_reconciled_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    op.create_check_constraint(
        "valid_transport",
        "agent_live_sessions",
        "transport IN ('ipc', 'poll')",
    )

    op.create_index(
        "ix_agent_live_sessions_daemon",
        "agent_live_sessions",
        ["daemon_id"],
    )
    op.create_index(
        "ix_agent_live_sessions_transport",
        "agent_live_sessions",
        ["transport", "last_reconciled_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_live_sessions_transport", table_name="agent_live_sessions")
    op.drop_index("ix_agent_live_sessions_daemon", table_name="agent_live_sessions")
    op.drop_constraint("valid_transport", "agent_live_sessions", type_="check")
    op.drop_column("agent_live_sessions", "last_reconciled_at")
    op.drop_column("agent_live_sessions", "daemon_id")
    op.drop_column("agent_live_sessions", "transport")
    op.drop_column("agent_live_sessions", "ipc_session_token")
