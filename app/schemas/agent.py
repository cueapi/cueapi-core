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
