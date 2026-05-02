"""Messaging primitive — multi-shell same-agent claims (PR-5a of Dock-readiness).

Adds the ``agent_shells`` table. Each row is one running process that
has registered to receive messages addressed to a given Agent.slug.
Same agent can have N concurrent shells (e.g., ``argus@govind`` running
in Claude Code AND Cursor on the same machine).

Push delivery fans out to all live shells. Poll-fetch returns the same
inbox to all shells; the SDK dedupes via Idempotency-Key on the agent
side (each shell sees the same message but only ONE acks it).

Schema overview:

* ``agent_shells`` — per-process registration. PK ``ash_<12 alphanum>``.
  Each shell has its own ``webhook_url`` + ``webhook_secret`` (so
  different shells can deliver to different local endpoints — Cursor's
  port, Claude Code's port, etc.).

* ``agent_shells.last_heartbeat_at`` — used to prune dead shells.
  A shell that hasn't heartbeat'd in N minutes is treated as offline
  (push delivery skips it; subsequent registrations may displace it).

The existing ``agents.webhook_url`` + ``agents.webhook_secret`` columns
are kept for backward compat. Treat them as the "legacy single-shell"
shape — when multi-shell is in use, those columns can be NULL and
shells own delivery. Service layer (in a follow-up PR or this same
deploy) reads from agent_shells when present.

Backward-compat contract:

* Additive only. No column on ``agents`` removed.
* ``agent_shells`` is empty initially → existing single-shell semantics
  preserved exactly. Existing tests pass without modification.
* Integrators who want multi-shell behavior register shells via
  ``POST /v1/agents/{ref}/shells`` and the service layer fans out.

Revision ID: 023
Revises: 022
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "023"
down_revision = "022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_shells",
        sa.Column("id", sa.String(length=20), primary_key=True),
        sa.Column(
            "agent_id",
            sa.String(length=20),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        # User scope is denormalized here so push-delivery + the
        # per-user concurrent-delivery cap can scope without a join
        # back to agents → users. Same source-of-truth FK chain.
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        # Per-shell delivery target. Mirrors agents.webhook_url shape.
        # NULL = poll-only shell (still gets inbox poll-fetch hits, no
        # push). webhook_secret is set/unset together with webhook_url
        # via the same paired-constraint pattern.
        sa.Column("webhook_url", sa.Text(), nullable=True),
        sa.Column("webhook_secret", sa.String(length=80), nullable=True),
        # Optional human label so admins can tell shells apart in
        # /agents/{ref}/shells listings ("claude-code on laptop",
        # "cursor on desktop", etc.).
        sa.Column("label", sa.String(length=128), nullable=True),
        # Presence + heartbeat. last_heartbeat_at is bumped on every
        # successful poll-fetch + on POST /v1/agents/{ref}/shells/{id}/heartbeat.
        # Stale shells (heartbeat > N minutes ago) are skipped by push
        # delivery and may be pruned by a periodic cleanup task.
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'online'"),
        ),
        sa.Column(
            "last_heartbeat_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('online', 'offline', 'away')",
            name="valid_shell_status",
        ),
        sa.CheckConstraint(
            "(webhook_url IS NULL) = (webhook_secret IS NULL)",
            name="shell_webhook_url_secret_paired",
        ),
    )

    # Most-common push-delivery query: live shells of a given agent
    # ordered by heartbeat freshness so push retries the freshest first.
    op.create_index(
        "ix_agent_shells_active",
        "agent_shells",
        ["agent_id", "status", "last_heartbeat_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_shells_active", table_name="agent_shells")
    op.drop_table("agent_shells")
