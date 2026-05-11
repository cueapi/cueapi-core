"""Event — append-only event log row (PR-1b event-emit primitive).

Per the Cue Messages design lock D1 (Mike 2026-05-10): the messaging
service emits a canonical delivery event on this primitive (PR-2a
wiring); subscribers consume via pull or push. Same row drives both
surfaces.

BIGSERIAL ``id`` is the monotonic cursor for pull pagination — see
migration 058's docstring for the 92-year-ceiling rationale.

This module ships DORMANT in PR-1b: model + table exist but no
caller emits to it until PR-2a wires the messaging service. Zero
production behavior change at PR-1b merge.

See ``app/models/subscription.py`` for the intent side (which agents
want which event types) and ``app/services/events_service.py`` for
the emit + pull service surface.
"""
from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.database import Base


class Event(Base):
    __tablename__ = "events"
    # Indexes mirror migration 058. Declared on the model so
    # ``Base.metadata.create_all`` (test conftest path) builds them —
    # pytest doesn't run ``alembic upgrade head``. Same pattern as
    # ``AgentLiveSession`` (caught in CI 2026-05-06).
    __table_args__ = (
        Index("ix_events_recipient_id_cursor", "recipient_agent_id", "id"),
        Index(
            "ux_events_idempotency_key",
            "event_type",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        # Full index on emitted_at — partial-predicate with NOW()
        # rejected by Postgres (functions in index predicates must be
        # IMMUTABLE; NOW() is STABLE). Cleanup query's range scan is
        # fast enough.
        Index("ix_events_emitted_at", "emitted_at"),
        # Phase 4b — partial index on un-digested rows for the
        # digest emitter's "find un-digested events for recipient X"
        # query. Mirrors migration 060's CONCURRENTLY index.
        Index(
            "ix_events_undigested",
            "recipient_agent_id",
            postgresql_where=text("digested_at IS NULL"),
        ),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    event_type = Column(Text, nullable=False)
    # FK type matches ``agents.id`` String(20) — the ``agt_xxx`` opaque
    # ID format. Same constraint as ``agent_live_sessions``.
    recipient_agent_id = Column(
        String(20),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
    )
    payload = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    emitted_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    # Optional emitter-supplied dedup key; partial-unique index above
    # makes re-emit with the same (event_type, key) a no-op that returns
    # the existing row.
    idempotency_key = Column(Text, nullable=True)
    # Phase 4b (migration 060) — digest batching watermark. Set when
    # a `message.delivered` event of priority 1 or 2 is bundled into
    # a `message.digest` event. The periodic digest emitter only
    # acts on rows where digested_at IS NULL.
    digested_at = Column(DateTime(timezone=True), nullable=True)
