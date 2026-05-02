"""AgentShell — per-process registration for multi-shell agents (PR-5a).

Each row is one running process that has registered to receive
messages addressed to a given ``Agent.slug``. The same agent can have
N concurrent shells — e.g., ``argus@govind`` running in Claude Code
AND Cursor AND OpenClaw simultaneously, each delivering to its own
local webhook port.

Why this exists
---------------

The original v1 messaging spec §2.3 has slugs lock-after-set with one
``webhook_url`` per agent. That's correct for the single-process
mental model but breaks when the SAME agent identity runs in multiple
shells on the same machine — a common pattern for Dock Connect users
running multiple AI tools side-by-side.

This table lets that work: one canonical Agent identity, N live shells
each holding its own webhook target. Push delivery fans out; the
SDK dedupes message handling at the agent layer via the existing
Idempotency-Key path.

Status field (online / offline / away) is the same vocabulary as
``agents.status`` — shells inherit the presence concept from agents.
A shell that hasn't heartbeat'd within
``MESSAGE_DELIVERY_STALE_AFTER_SECONDS`` is treated as offline by
push delivery (skipped). A periodic cleanup task can hard-delete
shells that have been offline > N hours.
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
    func,
)
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class AgentShell(Base):
    __tablename__ = "agent_shells"

    id = Column(String(20), primary_key=True)
    agent_id = Column(
        String(20),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    webhook_url = Column(Text, nullable=True)
    webhook_secret = Column(String(80), nullable=True)
    label = Column(String(128), nullable=True)
    status = Column(String(16), nullable=False, default="online", server_default="online")
    last_heartbeat_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    registered_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('online', 'offline', 'away')",
            name="valid_shell_status",
        ),
        CheckConstraint(
            "(webhook_url IS NULL) = (webhook_secret IS NULL)",
            name="shell_webhook_url_secret_paired",
        ),
        Index(
            "ix_agent_shells_active",
            "agent_id",
            "status",
            "last_heartbeat_at",
        ),
    )
