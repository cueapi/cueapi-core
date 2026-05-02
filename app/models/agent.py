"""Agent — first-class addressable Identity for the messaging primitive.

Spec: `https://trydock.ai/mike/cueapi-messaging-primitive-v1-sp` §2 (Identity primitive).

Each row is one Identity; messaging operations route through agents.
Cues stay scoped to users; messages route through agents on top of
the same multi-tenant model.

Key fields:

* ``id`` — opaque ``agt_<12 alphanum>`` PK. Mirrors ``cue_<12>``
  format from ``app.utils.ids.generate_cue_id``.
* ``user_id`` — tenancy boundary (per-user multi-tenancy from v1;
  v2 introduces an Org primitive that becomes the tenancy boundary
  while ``user_id`` becomes "creator/owner" — see §1.3a).
* ``slug`` — per-user unique slug. Used in ``agent@user`` slug-form
  addressing (§6 D11). Lock-after-set in v1 (§13 D3); v2 adds
  Gmail-style aliasing via a separate ``agent_aliases`` table.
* ``webhook_url`` / ``webhook_secret`` — paired (constraint
  ``agent_webhook_url_secret_paired``). NULL means "poll-only,
  no push." When set, push delivery uses the ``deliver_message``
  task_type via ``dispatch_outbox`` (§5).
* ``deleted_at`` — soft-delete tombstone. Hard-delete runs 30 days
  later via cleanup task (mirrors api_keys revocation pattern).
  ON DELETE SET NULL on FKs (``messages.from_agent_id``,
  ``messages.to_agent_id``) preserves message history when the
  agent record is hard-deleted.

The DB column ``agent_metadata`` is mapped to the ORM attribute
``metadata_`` (with trailing underscore) because ``metadata`` is a
SQLAlchemy reserved attribute on declarative ``Base``. API surface
exposes the field as ``metadata`` via the Pydantic schema layer.
"""
from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.database import Base


class Agent(Base):
    __tablename__ = "agents"

    id = Column(String(20), primary_key=True)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # NOTE: per-key visibility (``api_key_id`` FK to ``api_keys``) is a
    # HOSTED_ONLY feature in private cueapi (multi-key sprint, migration
    # 039). cueapi-core uses the legacy single-key model on
    # ``users.api_key_hash``, so ``api_key_id`` is omitted here. If a
    # self-host integrator needs per-key scoping they can add the
    # column + table in a follow-up migration that downstream-of this
    # one (the HOSTED_ONLY boundary is documented in HOSTED_ONLY.md).
    slug = Column(String(64), nullable=False)
    display_name = Column(String(255), nullable=False)
    webhook_url = Column(Text, nullable=True)
    webhook_secret = Column(String(80), nullable=True)
    # ``metadata_`` ORM attribute (with trailing underscore) maps to
    # the ``agent_metadata`` DB column. ``metadata`` is reserved by
    # SQLAlchemy on declarative Base. API surface exposes as
    # ``metadata`` via the Pydantic schema layer.
    metadata_ = Column(
        "agent_metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    status = Column(String(16), nullable=False, default="online", server_default="online")
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("user_id", "slug", name="unique_user_agent_slug"),
        CheckConstraint(
            "status IN ('online', 'offline', 'away')",
            name="valid_agent_status",
        ),
        CheckConstraint(
            "(webhook_url IS NULL) = (webhook_secret IS NULL)",
            name="agent_webhook_url_secret_paired",
        ),
        Index("ix_agents_slug", "slug"),
    )

