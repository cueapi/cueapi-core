"""Item 1 Option 1 — subscriptions.inline_body opt-in column.

Lets a subscriber opt into receiving the source message body
embedded in the event payload, eliminating the extra GET
``/v1/messages/{id}`` round-trip on the consumer side. Default
False preserves v1 behavior (META-only events).

Architecturally additive to CMA's Option 2 (runtime-side body-detect
+ skip-fetch). Both ship per CTO direction 2026-05-11 (Right > Easy
applied broadly).

**32KB cap** at emit time:

- Bodies ≤32KB are embedded as ``payload.body``.
- Bodies >32KB are omitted; ``payload.body_omitted =
  "size_too_large"`` and ``payload.body_size_bytes = <N>``. Consumer
  falls back to GET /v1/messages/{id} for the full body.

Empirical justification (staging Railway data 2026-05-11):

- ``cues.payload``: 99.94% of payloads ≤65 bytes (1830/1831 ≤ 1KB;
  one 1MB outlier handled by the omit-flag path)
- ``executions.outcome_result``: 100% ≤30 bytes
- Slack-style P99 message body ≈ 10KB; comfortably under 32KB

Resolves Backlog row cmp1j1rzs00020 (CTO concur 2026-05-11).

Revision ID: 033
Revises: 032
"""
from alembic import op
import sqlalchemy as sa


revision = "033"
down_revision = "032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column(
            "inline_body",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("subscriptions", "inline_body")
