"""Agent Directory productization (Phase A) — implicit online-state derivation.

Ports cueapi/cueapi#630 (private migration 048) to OSS.

Adds ``agents.last_seen_at`` so the server can derive an agent's
``online`` status from recent activity instead of requiring callers
to ``PATCH /v1/agents/{ref}`` with explicit status updates.

Hot paths that write ``last_seen_at = now()`` (added in this PR's
service-layer changes):

* ``create_message`` — sender's agent
* ``list_inbox`` — recipient's agent (poll-based delivery)

Derivation rules (computed in the service layer, not stored):

* ``last_seen_at`` within 5 min   → ``online``
* ``last_seen_at`` within 30 min  → ``away``
* anything older / NULL           → ``offline``

The existing ``status`` column stays as a caller-overrideable enum;
the new derivation is the default surface. Callers can still assert
``status=away`` (e.g., agent voluntarily marks itself away during a
long-running task) and the override wins over the derivation.

Migration sequence: OSS HEAD at branch creation was 023. Open PR #46
(message send_at) also targets 024; one of the two PRs will land first
and the second will need renumber to 025 — sentinel-rebase / manual
rebase resolves the collision.

Revision ID: 024
Revises: 023
"""
from alembic import op
import sqlalchemy as sa


revision = "024"
down_revision = "023"


def upgrade():
    op.add_column(
        "agents",
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_column("agents", "last_seen_at")
