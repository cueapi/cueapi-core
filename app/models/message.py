"""Message — ephemeral, immutable, identity-addressed communication.

Spec: `https://trydock.ai/mike/cueapi-messaging-primitive-v1-sp` §3 (Message primitive)
+ §4 (delivery state machine) + §8 (Idempotency-Key).

Each message is a single addressed event from one Agent to another.
Same-tenant constraint (sender.user_id == recipient.user_id) enforced
at create time in the service layer; v1 has no cross-tenant messaging
(§1.3a + §13 D4).

Key fields:

* ``id`` — opaque ``msg_<12 alphanum>`` PK.
* ``thread_id`` — server-issued. Root messages have ``thread_id = id``;
  replies inherit the root's thread_id via the ``reply_to`` chain.
* ``preview`` — first ~200 chars of body, server-computed at create
  time (§3 R3 dock-demo add). Lets inbox-list UIs render previews
  without fetching multi-KB bodies.
* ``priority`` — 1-5, default 3, with rate-limiting on >3 (§7).
* ``delivery_state`` — see §4.1 state machine: queued → delivering →
  delivered → read → acked (terminal) | retry_ready → failed | expired.
  ``failed`` is NOT terminal for fetchability — recipient can still
  poll-fetch failed messages.
* ``idempotency_key`` / ``idempotency_fingerprint`` — §8. Partial
  unique index on ``(user_id, idempotency_key) WHERE idempotency_key
  IS NOT NULL``. 24h dedup window enforced at app layer + cleanup
  task nulls keys older than 24h (PostgreSQL doesn't support NOW()
  in partial-index predicates; see §8.4).
* ``expires_at`` — 30-day default TTL (§13 D7); cleanup task transitions
  to ``expired`` once now() > expires_at; hard-delete 7 days later
  (§13 D10).

The DB column ``message_metadata`` maps to ORM attribute ``metadata_``
(SQLAlchemy reserved-name dodge, same shape as Agent.metadata_).
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
    # from_api_key_id (FK → api_keys) omitted — multi-key is HOSTED_ONLY.
    # See agent.py for the same omission rationale.
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
    # Slice 3b (Phase 12.1.5): set when worker claims (queued→delivering
    # OR retry_ready→delivering). Cleared back to NULL on terminal
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
    )

