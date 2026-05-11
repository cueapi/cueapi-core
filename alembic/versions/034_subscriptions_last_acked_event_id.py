"""Item 2(b) — subscriptions.last_acked_event_id watermark column.

Strategic v0.2 ack primitive. Separates "dispatched" (substrate
attempted delivery) from "acked" (consumer-side acknowledgement)
so callers can distinguish processed-vs-polled state.

**Two advancement paths**:

1. **Implicit (cursor advance)** — when a consumer pulls events via
   ``GET /v1/agents/{ref}/events``, the response carries a
   ``next_cursor``. The substrate interprets seeing those events
   as ack: pull-mode subs' ``last_acked_event_id`` advances to
   ``next_cursor``. Zero ergonomic cost for the consumer.

2. **Explicit** — ``PATCH /v1/agents/{ref}/subscriptions/{id}/ack``
   with body ``{"acked_event_id": N}``. For webhook subscribers or
   pull consumers wanting to ack without polling new events.

For **webhook subs**, the dispatcher already advances
``last_dispatched_event_id`` on successful POST. As of Item 2(b),
the dispatcher ALSO advances ``last_acked_event_id`` at the same
time — successful webhook delivery is treated as ack.

**Use cases unlocked**:

- RPC ack tracking via ``correlation_id`` roundtrip — sender can
  query if recipient acked their request
- Reply-chain status — distinguish "delivered but not yet acked"
  from "acked"
- Processed-vs-polled distinction — substrate can tell which events
  the consumer has actually progressed past

Resolves Backlog row cmp1j1vlp00060 (CTO concur 2026-05-11).

Revision ID: 034
Revises: 033
"""
from alembic import op
import sqlalchemy as sa


revision = "034"
down_revision = "033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column("last_acked_event_id", sa.BigInteger, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("subscriptions", "last_acked_event_id")
