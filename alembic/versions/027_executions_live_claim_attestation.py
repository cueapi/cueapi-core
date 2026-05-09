"""Live-claim attestation columns on executions.

Adds two columns to the ``executions`` table to support the
``POST /v1/executions/{id}/live-claim`` cmotigtnx attestation
endpoint introduced alongside the agent_live_sessions schema port:

* ``live_claim_session_token`` (VARCHAR 128, nullable) — the
  Crockford-base32 ULID written by the local claim-watcher when a
  Live session wins a claim race. Cross-references the ULID stored
  in ``agent_live_sessions.session_token`` (column added in
  migration 026).
* ``live_claimed_at`` (TIMESTAMPTZ, nullable) — server stamp at
  the moment the attestation was first recorded. Write-once
  (subsequent calls with the same token are idempotent; subsequent
  calls with a *different* token return 409 ``already_attested``).

Why an attestation column rather than just trusting handler-reported
``metadata.executed_via='live'``: bg-spawn ``claude --print``
subprocesses confabulating from auto-memory can fabricate a
"executed via live" claim that looks correct on paper but didn't
actually run through the user's local Live session. The attestation
is written by the local claim-watcher (which has the genuine ULID)
at atomic-mv time on a real claim; bg-spawn agents can't forge it
because they never see the local claim-watcher's ULID.

The outcome validator (in a follow-up that lives alongside the
existing outcome service) checks for the attestation when a handler
reports ``executed_via='live'`` and rejects the report if the
attestation is missing or mismatches. Phase 1 grace period accepts
bare ``executed_via='live'`` on executions whose Live-claim metadata
predates this migration; Phase 2 hard-rejects.

Both columns are nullable + additive; existing executions get NULL
on both, which is the intended state for non-Live deliveries.

Revision ID: 027
Revises: 026
"""
from alembic import op
import sqlalchemy as sa


revision = "027"
down_revision = "026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "executions",
        sa.Column(
            "live_claim_session_token",
            sa.String(length=128),
            nullable=True,
        ),
    )
    op.add_column(
        "executions",
        sa.Column(
            "live_claimed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("executions", "live_claimed_at")
    op.drop_column("executions", "live_claim_session_token")
