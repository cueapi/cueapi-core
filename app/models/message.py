"""Message — ephemeral, immutable, identity-addressed communication.

Each message is a single addressed event from one Agent to another.
Same-tenant constraint (sender.user_id == recipient.user_id) enforced
at create time in the service layer; v1 has no cross-tenant messaging.

Key fields:

* ``id`` — opaque ``msg_<12 alphanum>`` PK.
* ``thread_id`` — server-issued. Root messages have ``thread_id = id``;
  replies inherit the root's thread_id via the ``reply_to`` chain.
* ``preview`` — first ~200 chars of body, server-computed at create
  time. Lets inbox-list UIs render previews without fetching multi-KB
  bodies.
* ``priority`` — 1-5, default 3, with rate-limiting on >3.
* ``delivery_state`` — see state machine: queued → delivering →
  delivered → read → acked (terminal) | retry_ready → failed | expired.
  ``failed`` is NOT terminal for fetchability — recipient can still
  poll-fetch failed messages.
* ``idempotency_key`` / ``idempotency_fingerprint`` — Partial unique
  index on ``(user_id, idempotency_key) WHERE idempotency_key IS NOT
  NULL``. 24h dedup window enforced at app layer + cleanup task nulls
  keys older than 24h (PostgreSQL doesn't support NOW() in partial-
  index predicates).
* ``expires_at`` — 30-day default TTL; cleanup task transitions to
  ``expired`` once now() > expires_at.

The DB column ``message_metadata`` maps to ORM attribute ``metadata_``
(SQLAlchemy reserved-name dodge, same shape as Agent.metadata_).

OSS port note: ``from_api_key_id`` column omitted — multi-key scoping
not present in cueapi-core. See migration 021's docstring.
"""
from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.database import Base


class Message(Base):
    __tablename__ = "messages"

    id = Column(String(20), primary_key=True)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    from_agent_id = Column(
        String(20),
        ForeignKey("agents.id", ondelete="SET NULL"),
        nullable=False,
        index=True,
    )
    to_agent_id = Column(
        String(20),
        ForeignKey("agents.id", ondelete="SET NULL"),
        nullable=False,
        index=True,
    )
    thread_id = Column(String(20), nullable=False, index=True)
    reply_to = Column(
        String(20),
        ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    subject = Column(String(255), nullable=True)
    body = Column(Text, nullable=False)
    preview = Column(String(256), nullable=False, default="", server_default="")
    priority = Column(SmallInteger, nullable=False, default=3, server_default="3")
    expects_reply = Column(Boolean, nullable=False, default=False, server_default="false")
    reply_to_agent_id = Column(
        String(20),
        ForeignKey("agents.id", ondelete="SET NULL"),
        nullable=True,
    )
    delivery_state = Column(
        String(16),
        nullable=False,
        default="queued",
        server_default="queued",
    )
    metadata_ = Column(
        "message_metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    idempotency_key = Column(String(255), nullable=True)
    idempotency_fingerprint = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    read_at = Column(DateTime(timezone=True), nullable=True)
    acked_at = Column(DateTime(timezone=True), nullable=True)
    failed_at = Column(DateTime(timezone=True), nullable=True)
    # Slice 3b: set when worker claims (queued→delivering OR
    # retry_ready→delivering). Cleared back to NULL on terminal
    # transitions (delivered/failed/expired). Powers the stale-recovery
    # poll loop — a message stuck in ``delivering`` past the stale
    # threshold gets moved back to ``retry_ready`` so the dispatcher
    # can re-enqueue. Handles worker-crash-mid-delivery.
    delivering_started_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),  # default-to-creation; service-layer adds 30d at create time
    )

    # PR-2a-OSS port (migration 029) — event-emit primitive wiring columns.
    # Verbatim port of private cueapi's columns from migration 059. Same
    # semantics: bucket computed at create from priority (lets server-side
    # dispatcher batch by tier without re-querying); message_dispatch_error
    # captures handler-error context (EC1: outcome.error → message audit
    # trail); correlation_id is RPC framing (D3) for programmatic
    # request/response matching independent of reply_to chains.
    dispatch_priority_bucket = Column(
        SmallInteger,
        nullable=False,
        default=3,
        server_default="3",
    )
    message_dispatch_error = Column(Text, nullable=True)
    correlation_id = Column(String(255), nullable=True)

    __table_args__ = (
        CheckConstraint("priority BETWEEN 1 AND 5", name="valid_priority"),
        CheckConstraint(
            "delivery_state IN ('queued','delivering','retry_ready','delivered','read','claimed','acked','expired','failed')",
            name="valid_delivery_state",
        ),
        CheckConstraint(
            "octet_length(body) <= 32768",
            name="body_size_limit",
        ),
        # Inbox-fetch (most common path): to_agent_id + state filter +
        # created_at DESC ordering.
        Index(
            "ix_messages_inbox",
            "to_agent_id",
            "delivery_state",
            "created_at",
        ),
        Index("ix_messages_sent", "from_agent_id", "created_at"),
        Index("ix_messages_thread", "thread_id", "created_at"),
        # PR-2a-OSS port — partial index for RPC correlation lookup.
        # Mirrors migration 029's CONCURRENTLY index so
        # Base.metadata.create_all builds it in tests.
        Index(
            "ix_messages_correlation_id",
            "correlation_id",
            postgresql_where=text("correlation_id IS NOT NULL"),
        ),
    )
