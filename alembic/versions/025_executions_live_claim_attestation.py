"""Live-claim attestation columns on executions.

Parity port of cueapi/cueapi#664 (P0 Bulletproofing — backlog
``cmotigtnx``). Self-hosters running their own Live-attached agents
(claude-code, cursor, openclaw, etc. with a CueAPI Desktop-style
claim-watcher OR a plain ``tail -F worker.log | grep "Claimed
execution"`` pattern) need the same attestation gate to detect
BG-spawn fabrication of ``executed_via='live'`` outcomes.

Background: a BG-spawn ``claude --print`` (or equivalent) subprocess
that has access to the same auto-memory as the Live session can
self-report ``metadata.executed_via='live'`` from auto-memory even
when the cue actually ran in the background. ``claimed_by_worker`` is
the daemon's identity for ALL execs (Live + BG indistinguishable) so
the server has had no INDEPENDENT signal to verify a Live claim.

The fix: a new endpoint ``POST /v1/executions/{id}/live-claim`` that
the local claim-watcher hits at atomic-mv time with the agent's
session token. The server records the attestation on the execution
row. The outcome validator then requires an attestation for
``executed_via='live'`` outcomes — fabricated claims without a
matching attestation are rejected.

Two columns:

* ``live_claim_session_token`` VARCHAR(128) — opaque string from the
  agent's attach record. Validated for non-empty + size only at this
  layer; future hardening (look up in an attach_records table)
  doesn't need a schema change.
* ``live_claimed_at`` TIMESTAMPTZ — first attestation wins (the
  attestation endpoint is one-shot per execution).

Both nullable adds, no backfill. Existing executions remain
unattested (``live_claimed_at IS NULL``) and the validator only
fires on outcomes that explicitly report ``executed_via='live'``;
silent absence isn't penalized.

Revision ID: 024
Revises: 023
"""
from alembic import op
import sqlalchemy as sa


revision = "025"
down_revision = "024"


def upgrade():
    op.add_column(
        "executions",
        sa.Column("live_claim_session_token", sa.String(128), nullable=True),
    )
    op.add_column(
        "executions",
        sa.Column("live_claimed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_column("executions", "live_claimed_at")
    op.drop_column("executions", "live_claim_session_token")
