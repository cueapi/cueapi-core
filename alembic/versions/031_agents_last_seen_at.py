"""Agent Directory productization (Phase A) — implicit online-state derivation.

Adds ``agents.last_seen_at`` so the server can derive an agent's
``online`` status from recent activity instead of requiring callers
to ``PATCH /v1/agents/{ref}`` with explicit status updates.

Hot paths that write ``last_seen_at = now()`` (added in this PR's
service-layer changes):

* ``create_message`` — sender's agent
* ``list_inbox`` — recipient's agent (poll-based delivery)
* push ``deliver_message`` worker callback — recipient's agent

Derivation rules (computed in the service layer, not stored):

* ``last_seen_at`` within 5 min   → ``online``
* ``last_seen_at`` within 30 min  → ``away``
* anything older / NULL           → ``offline``

The existing ``status`` column stays as a caller-overrideable enum;
the new derivation is the default surface. Callers can still assert
``status=away`` (e.g., agent voluntarily marks itself away during a
long-running task) and the override wins over the derivation.

Migration 047 was the last messaging-related migration (per-message
send_at). 048 is independent of messaging — it shapes the Identity
primitive directly.

Revision ID: 031
Revises: 030
"""
from alembic import op
import sqlalchemy as sa


revision = "031"
down_revision = "030"


def upgrade():
    # Nullable add — no backfill required. NULL means "no activity
    # observed yet" which the derivation maps to ``offline``. Existing
    # rows keep their caller-asserted status until the first hot-path
    # write lands.
    op.add_column(
        "agents",
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    )
    # No index needed — read path is per-tenant and per-agent, both
    # already covered by the existing ``unique_user_agent_slug`` and
    # ``ix_agents_slug`` indexes. The roster endpoint reads
    # ``user_id``-scoped rows, which is already a btree-indexed FK.


def downgrade():
    op.drop_column("agents", "last_seen_at")
