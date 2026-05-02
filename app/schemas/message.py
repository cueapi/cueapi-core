"""Pydantic schemas for the Message primitive.

Surface notes:

* ORM stores ``Message.metadata_`` (DB column ``message_metadata``);
  the API exposes the field as ``metadata`` per the spec.
* `body` capped at 32,768 bytes via Pydantic + DB CheckConstraint
  backstop. `metadata` JSON capped at 10 KB at the service layer.
* `to` / `reply_to_agent` accept either opaque agent_id (`agt_xxx`)
  or slug-form (`agent_slug@user_slug`) — service layer's
  ``resolve_address`` translates either form to an Agent row.

OSS port note: ``from_api_key_id`` field omitted from MessageResponse —
multi-key scoping not present in cueapi-core.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class MessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: str = Field(..., min_length=1, description="Recipient: opaque agent_id or slug-form (agent@user)")
    body: str = Field(..., min_length=1, max_length=32768)
    subject: Optional[str] = Field(default=None, max_length=255)
    reply_to: Optional[str] = Field(
        default=None,
        pattern=r"^msg_[a-z0-9]{12}$",
        description="Explicit previous-message reference; thread_id inherited from this message.",
    )
    priority: int = Field(default=3, ge=1, le=5)
    expects_reply: bool = False
    reply_to_agent: Optional[str] = Field(
        default=None,
        min_length=1,
        description="Decoupled reply target. Null = reply to `from` (default).",
    )
    metadata: Dict = Field(default_factory=dict)


class FromAgentRef(BaseModel):
    """Minimal agent reference rendered inline on message responses."""

    agent_id: Optional[str]
    slug: Optional[str]


class MessageResponse(BaseModel):
    id: str
    user_id: str
    from_agent_id: Optional[str]
    to_agent_id: Optional[str]
    thread_id: str
    reply_to: Optional[str]
    subject: Optional[str]
    body: str
    preview: str
    priority: int
    expects_reply: bool
    reply_to_agent_id: Optional[str]
    delivery_state: Literal[
        "queued",
        "delivering",
        "retry_ready",
        "delivered",
        "read",
        "claimed",
        "acked",
        "expired",
        "failed",
    ]
    metadata: Dict
    idempotency_key: Optional[str]
    created_at: datetime
    delivered_at: Optional[datetime]
    read_at: Optional[datetime]
    acked_at: Optional[datetime]
    failed_at: Optional[datetime]
    expires_at: datetime


class MessageListResponse(BaseModel):
    messages: List[MessageResponse]
    total: int
    limit: int
    offset: int


class CountResponse(BaseModel):
    """Returned when ``GET /v1/agents/{ref}/inbox?count_only=true``."""

    count: int


class StateTransitionResponse(BaseModel):
    """Returned by /read and /ack — minimal state-only response."""

    delivery_state: str
    read_at: Optional[datetime] = None
    acked_at: Optional[datetime] = None
