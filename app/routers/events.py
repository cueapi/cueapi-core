"""Event-emit primitive — HTTP routes (PR-1b).

Thin wrappers over ``app/services/events_service.py``. Auth via
``get_current_user``; agent resolution via ``get_agent_owned`` so
the route enforces the §Authorization rule from the PR-1b spec:
**subscriptions are agent-scoped — an agent can only subscribe to
events FOR ITSELF**.

Endpoints:

* ``POST   /v1/agents/{ref}/subscriptions`` — create. Body:
  ``{event_type, delivery_target, webhook_url?}``. Returns 201 with
  the subscription, plus ``webhook_secret`` one-shot for webhook subs.
* ``GET    /v1/agents/{ref}/subscriptions`` — list active subs.
  Each entry includes dispatch-state fields (``last_dispatched_event_id``
  etc) per CTO correction #2. ``webhook_url`` redacted to host-only;
  ``webhook_secret`` never exposed here.
* ``DELETE /v1/agents/{ref}/subscriptions/{id}`` — soft-detach.
  Idempotent — re-DELETE returns 200 regardless of whether the row
  was already detached.
* ``GET    /v1/agents/{ref}/events`` — pull events stream. Query
  params: ``since`` (cursor, default 0), ``limit`` (default 100,
  max 1000 server-side), ``event_type`` (optional filter).

DORMANT shape — substrate ships in this PR; no caller emits events
until PR-2a wires the messaging service.

Errors:

The service layer raises typed ``EventsServiceError`` subclasses
that the route layer translates to standard CueAPI error responses
``{"error": {"code", "message", "status"}}``. Validation failures
return 400 with specific codes (``unknown_event_type``,
``invalid_delivery_target``, ``invalid_webhook_url``); not-found
returns 404 (``subscription_not_found``); authorization failures
on the parent agent return 404 (``agent_not_found``, matches
existing pattern — don't leak existence of other users' agents).
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthenticatedUser, get_current_user
from app.database import get_db
from app.models.event import Event
from app.models.subscription import Subscription
from app.services.agent_service import get_agent_owned
from app.services.events_service import (
    EventsServiceError,
    create_subscription,
    detach_subscription,
    list_subscriptions,
    pull_events,
)

router = APIRouter(prefix="/v1/agents", tags=["events"])


# ───────────────────────────────────────────────────────────────────────
# Schemas
# ───────────────────────────────────────────────────────────────────────


class SubscriptionCreate(BaseModel):
    """Body for POST /v1/agents/{ref}/subscriptions."""

    model_config = ConfigDict(extra="forbid")

    event_type: str = Field(..., min_length=1, max_length=255)
    delivery_target: Literal["pull", "webhook"]
    webhook_url: Optional[str] = Field(default=None, max_length=2048)


class SubscriptionResponse(BaseModel):
    """Response shape for create + list + detail endpoints.

    Per CTO correction #2: list responses include dispatch-state
    surface so recipients can observe paused-webhook state.
    ``webhook_url`` is redacted to scheme + host before responding
    (full URL only stored server-side). ``webhook_secret`` appears
    ONLY on the create response and only for webhook subs."""

    id: str
    subscriber_agent_id: str
    event_type: str
    delivery_target: str
    webhook_url: Optional[str] = None
    webhook_secret: Optional[str] = None
    last_dispatched_event_id: Optional[int] = None
    last_dispatched_at: Optional[str] = None
    consecutive_failures: int = 0
    paused_until: Optional[str] = None
    created_at: str
    detached_at: Optional[str] = None


class SubscriptionListResponse(BaseModel):
    subscriptions: List[SubscriptionResponse]


class EventResponse(BaseModel):
    """One row in the pull events stream."""

    id: int
    event_type: str
    recipient_agent_id: str
    payload: Dict[str, Any]
    emitted_at: str


class EventListResponse(BaseModel):
    events: List[EventResponse]
    next_cursor: Optional[int] = None
    has_more: bool = False


# ───────────────────────────────────────────────────────────────────────
# Helpers — pure transforms; safe to unit-test in isolation.
# ───────────────────────────────────────────────────────────────────────


def _redact_webhook_url(url: Optional[str]) -> Optional[str]:
    """Strip query params + path; keep scheme + host. Prevents leaking
    secrets-in-URL (e.g. embedded webhook auth tokens) on list responses.

    Returns ``None`` if input is None or unparseable.
    """
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001 — defensive: malformed URL → no surface
        return None
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _subscription_to_response(
    sub: Subscription,
    *,
    include_secret: bool = False,
) -> SubscriptionResponse:
    """Map a Subscription row to the wire shape.

    ``include_secret=True`` ONLY on the create endpoint (one-shot
    reveal at create time); all other responses pass False so the
    secret never appears in list / detail surfaces.
    """
    return SubscriptionResponse(
        id=str(sub.id),
        subscriber_agent_id=sub.subscriber_agent_id,
        event_type=sub.event_type,
        delivery_target=sub.delivery_target,
        webhook_url=_redact_webhook_url(sub.webhook_url),
        webhook_secret=sub.webhook_secret if include_secret else None,
        last_dispatched_event_id=sub.last_dispatched_event_id,
        last_dispatched_at=(
            sub.last_dispatched_at.isoformat() if sub.last_dispatched_at else None
        ),
        consecutive_failures=sub.consecutive_failures,
        paused_until=sub.paused_until.isoformat() if sub.paused_until else None,
        created_at=sub.created_at.isoformat() if sub.created_at else "",
        detached_at=sub.detached_at.isoformat() if sub.detached_at else None,
    )


def _event_to_response(event: Event) -> EventResponse:
    return EventResponse(
        id=event.id,
        event_type=event.event_type,
        recipient_agent_id=event.recipient_agent_id,
        payload=event.payload or {},
        emitted_at=event.emitted_at.isoformat() if event.emitted_at else "",
    )


def _service_error_to_http(exc: EventsServiceError) -> HTTPException:
    """Translate a typed service error to the CueAPI HTTPException shape."""
    return HTTPException(
        status_code=exc.status,
        detail={
            "error": {
                "code": exc.code,
                "message": str(exc),
                "status": exc.status,
            }
        },
    )


# ───────────────────────────────────────────────────────────────────────
# Routes
# ───────────────────────────────────────────────────────────────────────


@router.post(
    "/{ref}/subscriptions",
    response_model=SubscriptionResponse,
    status_code=201,
)
async def create_subscription_endpoint(
    ref: str,
    body: SubscriptionCreate,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a subscription for an agent.

    The ``{ref}`` path param resolves to an agent owned by the
    authenticated user; ``subscriber_agent_id`` is stamped from the
    resolved row. Caller cannot inject a foreign id (the body
    schema doesn't accept it).
    """
    agent = await get_agent_owned(db, user, ref)
    try:
        sub = await create_subscription(
            db,
            subscriber_agent_id=agent.id,
            event_type=body.event_type,
            delivery_target=body.delivery_target,
            webhook_url=body.webhook_url,
        )
    except EventsServiceError as exc:
        raise _service_error_to_http(exc) from exc
    await db.commit()
    return _subscription_to_response(sub, include_secret=True)


@router.get(
    "/{ref}/subscriptions",
    response_model=SubscriptionListResponse,
)
async def list_subscriptions_endpoint(
    ref: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List active subscriptions for an agent.

    Includes dispatch-state fields so recipients can observe
    paused-webhook state. ``webhook_url`` host-redacted;
    ``webhook_secret`` omitted entirely.
    """
    agent = await get_agent_owned(db, user, ref)
    subs = await list_subscriptions(db, subscriber_agent_id=agent.id)
    return SubscriptionListResponse(
        subscriptions=[_subscription_to_response(s) for s in subs]
    )


@router.delete(
    "/{ref}/subscriptions/{subscription_id}",
    status_code=200,
)
async def delete_subscription_endpoint(
    ref: str,
    subscription_id: UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Soft-detach a subscription.

    Idempotent — returns 200 whether or not a row was updated. The
    service layer guards against cross-owner detach (returns False);
    the route response is the same either way (matches spec contract:
    "re-DELETE = 200").
    """
    agent = await get_agent_owned(db, user, ref)
    await detach_subscription(
        db,
        subscription_id=subscription_id,
        subscriber_agent_id=agent.id,
    )
    await db.commit()
    return {"detached": True}


@router.get(
    "/{ref}/events",
    response_model=EventListResponse,
)
async def pull_events_endpoint(
    ref: str,
    since: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    event_type: Optional[str] = Query(None, max_length=255),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Pull events for an agent.

    Cursor-based pagination: pass ``since=<last next_cursor>`` on the
    next call. Server-side limit cap at 1000 (FastAPI ``le=1000``
    enforces). v0.1 ships short-poll only; long-poll mode
    (``?wait=long`` via LISTEN/NOTIFY) reserved for follow-up
    commit in this PR.
    """
    agent = await get_agent_owned(db, user, ref)
    events, next_cursor, has_more = await pull_events(
        db,
        recipient_agent_id=agent.id,
        since=since,
        limit=limit,
        event_type=event_type,
    )
    return EventListResponse(
        events=[_event_to_response(e) for e in events],
        next_cursor=next_cursor,
        has_more=has_more,
    )
