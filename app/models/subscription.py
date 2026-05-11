"""Subscription — an agent's intent to receive events of a given type.

Pairs with ``app/models/event.py`` for the PR-1b event-emit primitive.

Two consumption surfaces (per Cue Messages design lock + PR-1b spec):

* ``delivery_target='pull'`` — universal floor. Agent polls
  ``GET /v1/agents/{ref}/events?since=<cursor>``. No server-side
  heartbeat; the poll IS the liveness signal.
* ``delivery_target='webhook'`` — opt-in latency optimization. Server
  POSTs events to ``webhook_url`` with HMAC signature. Server-side
  dispatch loop tracks ``last_dispatched_event_id`` watermark.

One active subscription per (agent, event_type, delivery_target)
tuple — but an agent CAN have both pull + webhook subs for the same
event_type (concurrent dual-surface, defensive doubling during
webhook outages).

Authorization rule (PR-1b spec §Authorization): subscriptions are
agent-scoped; an agent can only subscribe to events FOR ITSELF. The
``subscriber_agent_id`` here is stamped at route-level from the
authenticated user's owned agent — never accepted as a caller-
supplied override.

Circuit breaker for webhook subs: after 10 consecutive failures
(``consecutive_failures >= 10``), the dispatch loop sets
``paused_until = NOW() + 1h``. The pull surface still works for the
same agent (different subscription row).

See ``app/services/events_service.py`` for the subscribe / list /
detach / dispatch service surface.
"""
from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        CheckConstraint(
            "delivery_target IN ('pull', 'webhook')",
            name="subscriptions_delivery_target_check",
        ),
        CheckConstraint(
            "(delivery_target = 'pull' AND webhook_url IS NULL) OR "
            "(delivery_target = 'webhook' AND webhook_url IS NOT NULL)",
            name="subscriptions_webhook_url_check",
        ),
        # At most one active subscription per (agent, event_type, target).
        Index(
            "ux_subscriptions_active_unique",
            "subscriber_agent_id",
            "event_type",
            "delivery_target",
            unique=True,
            postgresql_where=text("detached_at IS NULL"),
        ),
        # Dispatch-due lookup for the webhook dispatch loop.
        # paused_until filter handled at query time — partial
        # predicates can't reference NOW() (IMMUTABLE-only).
        Index(
            "ix_subscriptions_dispatch_due",
            "last_dispatched_event_id",
            postgresql_where=text(
                "delivery_target = 'webhook' AND detached_at IS NULL"
            ),
        ),
    )

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    subscriber_agent_id = Column(
        String(20),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type = Column(Text, nullable=False)
    delivery_target = Column(Text, nullable=False)
    webhook_url = Column(Text, nullable=True)
    # ``whsec_`` + 64 hex chars; server-minted at create time for
    # webhook subs. Stored verbatim; presented to caller only once
    # in the create response (matches per-user webhook_secret rotate
    # pattern).
    webhook_secret = Column(Text, nullable=True)
    # NULL = never dispatched yet; first dispatch picks up all events
    # with id > 0 (= all events). Bumped to the highest event.id
    # successfully dispatched.
    last_dispatched_event_id = Column(BigInteger, nullable=True)
    last_dispatched_at = Column(DateTime(timezone=True), nullable=True)
    consecutive_failures = Column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    paused_until = Column(DateTime(timezone=True), nullable=True)
    # Item 1 Option 1 (migration 061, CTO concur 2026-05-11) — opt-in
    # body embedding. When True, emit_event includes the source
    # message body in payload.body (≤32KB) or sets a body_omitted
    # flag (>32KB). Default False preserves v1 META-only behavior.
    inline_body = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    detached_at = Column(DateTime(timezone=True), nullable=True)
