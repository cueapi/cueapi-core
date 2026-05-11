"""Pydantic schemas for the Agent (Identity) primitive.

Surface notes:

* ORM stores ``Agent.metadata_`` (mapped DB column ``agent_metadata``);
  the API exposes the field as ``metadata`` per the spec schema.
* ``webhook_secret`` is included ONLY in the response from
  ``POST /v1/agents`` and from the webhook-secret retrieval/rotation
  endpoints. ``GET`` and ``PATCH`` responses omit it.
* ``slug`` is set-once-then-locked — ``AgentUpdate`` rejects any
  attempt to set it via the ``extra="forbid"`` Pydantic config and
  the absence of a ``slug`` field on the update model.

OSS port note: ``api_key_id`` field omitted from AgentResponse —
multi-key scoping not present in cueapi-core.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# Slug regex — 1-64 chars, lowercase alphanumeric + hyphens, no
# leading/trailing hyphen. (Consecutive hyphens allowed for now.)
_SLUG_PATTERN = r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$"


class AgentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=_SLUG_PATTERN,
        description="Per-user unique slug. If omitted, server derives from display_name.",
    )
    display_name: str = Field(..., min_length=1, max_length=255)
    webhook_url: Optional[str] = Field(
        default=None,
        max_length=2048,
        description="Push-delivery target. SSRF-validated. Null = poll-only.",
    )
    metadata: Dict = Field(default_factory=dict)


class AgentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    webhook_url: Optional[str] = Field(default=None, max_length=2048)
    # Sentinel to allow explicit clearing: pass `null` to clear webhook_url.
    # Pydantic + Optional + None on PATCH means "either omit (no change)
    # or explicitly set null (clear)". We disambiguate using model_fields_set.
    status: Optional[Literal["online", "offline", "away"]] = None
    metadata: Optional[Dict] = None


class AgentResponse(BaseModel):
    id: str
    user_id: str
    slug: str
    display_name: str
    webhook_url: Optional[str]
    # Populated only on POST /v1/agents and on webhook-secret rotate.
    # GET/PATCH responses omit by setting webhook_secret=None.
    webhook_secret: Optional[str] = None
    metadata: Dict
    status: Literal["online", "offline", "away"]
    deleted_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


class AgentListResponse(BaseModel):
    agents: List[AgentResponse]
    total: int
    limit: int
    offset: int


class WebhookSecretResponse(BaseModel):
    """Response for the webhook-secret retrieval and rotation endpoints."""

    webhook_secret: str


class AgentRosterEntry(BaseModel):
    """One agent in the directory snapshot returned by GET /v1/agents/roster.

    Distinct from ``AgentResponse``: drops opaque IDs, secrets,
    timestamps, and tenancy metadata, and adds derived ``online`` /
    ``last_seen_relative`` fields. Optimized for prompt injection at
    session-boot — agents see "who else is here" natively without
    needing to call a tool. See PRD §Surface 5.
    """

    name: str = Field(..., description="Stable per-tenant slug; addressable as `<name>@<user_slug>`.")
    display_name: str
    description: Optional[str] = Field(default=None, description="From metadata.description if set.")
    online: bool = Field(..., description="Derived from last_seen_at within 5 min.")
    last_seen_relative: str = Field(
        ...,
        description="Human-readable freshness: 'active now', '5m ago', 'offline 2h', 'never'.",
    )
    preferred_contact: Literal["sync", "async"] = Field(
        ...,
        description="Derived: webhook_url IS NOT NULL → 'sync' (push-capable), else 'async' (poll-only).",
    )
    status: Literal["online", "offline", "away"] = Field(
        ...,
        description="Caller-asserted status (PATCH /v1/agents/{ref}); overrides derivation when explicit.",
    )


class AgentRosterResponse(BaseModel):
    """Response for GET /v1/agents/roster — full directory snapshot."""

    generated_at: datetime
    agents: List[AgentRosterEntry]
