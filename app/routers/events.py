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

import asyncio
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
    ack_subscription,
    advance_ack_watermark,
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
    inline_body: bool = Field(
        default=False,
        description=(
            "Item 1 Option 1 (CTO concur 2026-05-11): opt into body "
            "embedding. When True, emit_event includes the source "
            "message body in payload.body (≤32KB) or sets a body_omitted "
            "flag (>32KB). Default False preserves META-only v1 behavior. "
            "Coexists architecturally with consumer-side body-detect-and-"
            "skip-fetch — both paths are additive."
        ),
    )


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
    inline_body: bool = False
    last_dispatched_event_id: Optional[int] = None
    last_dispatched_at: Optional[str] = None
    last_acked_event_id: Optional[int] = None
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


class AckSubscriptionRequest(BaseModel):
    """Body for PATCH /v1/agents/{ref}/subscriptions/{id}/ack.

    Item 2(b) explicit-ack surface (CTO concur 2026-05-11). For
    webhook subscribers + pull consumers wanting to ack without
    pulling new events. Watermark monotonicity is server-enforced
    (no rewinds).
    """

    model_config = ConfigDict(extra="forbid")

    acked_event_id: int = Field(..., ge=0)


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
        inline_body=bool(sub.inline_body),
        last_dispatched_event_id=sub.last_dispatched_event_id,
        last_acked_event_id=sub.last_acked_event_id,
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
            inline_body=body.inline_body,
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


@router.patch(
    "/{ref}/subscriptions/{subscription_id}/ack",
    status_code=200,
)
async def ack_subscription_endpoint(
    ref: str,
    subscription_id: UUID,
    body: AckSubscriptionRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Item 2(b) — explicit ack for a subscription.

    Two cases this serves (vs. the implicit cursor-advance path
    on GET /events):

    * **Webhook subscribers** — they don't pull; their dispatch
      loop advances ack on successful POST automatically, but the
      explicit PATCH lets a downstream consumer-of-the-webhook
      flow back its own ack signal (e.g., recipient processed the
      message vs. just received it).
    * **Pull consumers needing to ack without polling new events**
      — rare but useful for "I've processed up to event N, but
      I'm not ready to pull more yet."

    Watermark monotonicity enforced server-side: this endpoint
    never moves the ack backwards. Returns 200 with
    ``{"acked": True}`` regardless of whether the update was a
    no-op (already-at-or-past-N) or an actual advance — observable
    end state is the same.
    """
    agent = await get_agent_owned(db, user, ref)
    await ack_subscription(
        db,
        subscription_id=subscription_id,
        subscriber_agent_id=agent.id,
        acked_event_id=body.acked_event_id,
    )
    await db.commit()
    return {"acked": True}


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


# Long-poll constants — tunable for tests + future optimization.
LONG_POLL_MAX_SECONDS = 30.0
LONG_POLL_INTERNAL_INTERVAL_SECONDS = 1.0


async def _advance_ack_after_pull(
    db: AsyncSession,
    *,
    agent_id: str,
    next_cursor: Optional[int],
    event_type: Optional[str],
) -> None:
    """Item 2(b) ack-advance side-effect for GET /v1/agents/{ref}/events.

    Extracted as a pure helper for two reasons:

    1. **Coverage**: pytest-cov on ASGI-dispatched paths doesn't
       reliably trace branches through FastAPI's async route wrapper
       (same pattern as ``_run_long_poll_wait``). Pulling the
       side-effect into a top-level coroutine makes coverage
       deterministic.
    2. **Testability**: direct ``await`` calls are simpler than
       orchestrating async background-event inserts via TestClient.

    Behavior:

    - If ``next_cursor`` is None (no events returned), no-op.
    - Else: call ``advance_ack_watermark`` to bulk-update matching
      pull subs' ``last_acked_event_id``. Wrapped try/except — ack
      write failure must NOT corrupt the consumer's read; the pull
      itself already succeeded.
    """
    if next_cursor is None:
        return
    try:
        await advance_ack_watermark(
            db,
            recipient_agent_id=agent_id,
            new_acked_event_id=next_cursor,
            event_type=event_type,
        )
        await db.commit()
    except Exception:  # noqa: BLE001 — read already succeeded; never bubble
        await db.rollback()


async def _run_long_poll_wait(
    db: AsyncSession,
    *,
    recipient_agent_id: str,
    since: int,
    limit: int,
    event_type: Optional[str],
) -> tuple[list, Optional[int], bool]:
    """Hold the connection open polling for new events until the
    window elapses OR an event arrives.

    Extracted as a pure async helper for two reasons:

    1. **Coverage** — pytest-cov on ASGI-dispatched paths doesn't
       reliably trace branches through FastAPI's async route
       wrapping. Pulling the loop body into a top-level coroutine
       makes coverage tracing deterministic + lets us unit-test the
       loop directly without spinning up the HTTP client.
    2. **Testability** — direct ``await _run_long_poll_wait(...)``
       calls are simpler than orchestrating async background-event
       inserts via the TestClient. The route stays a thin wrapper.

    Returns ``(events, next_cursor, has_more)`` — same shape as
    ``pull_events``. Empty list + None cursor + False has_more if
    the window elapsed without new events.

    Window + cadence read from module-level constants
    (``LONG_POLL_MAX_SECONDS`` + ``LONG_POLL_INTERNAL_INTERVAL_SECONDS``)
    so tests can monkeypatch shorter values without function
    signature churn.
    """
    deadline = asyncio.get_event_loop().time() + LONG_POLL_MAX_SECONDS
    events: list = []
    next_cursor: Optional[int] = None
    has_more = False

    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            break
        # Sleep up to the internal interval (or remaining time,
        # whichever is shorter) before re-polling.
        await asyncio.sleep(
            min(LONG_POLL_INTERNAL_INTERVAL_SECONDS, remaining)
        )
        events, next_cursor, has_more = await pull_events(
            db,
            recipient_agent_id=recipient_agent_id,
            since=since,
            limit=limit,
            event_type=event_type,
        )
        if events:
            break

    return events, next_cursor, has_more


@router.get(
    "/{ref}/events",
    response_model=EventListResponse,
)
async def pull_events_endpoint(
    ref: str,
    since: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    event_type: Optional[str] = Query(None, max_length=255),
    wait: Optional[Literal["long"]] = Query(
        default=None,
        description=(
            "Optional opt-in long-poll mode. When ``wait=long``, the "
            "server holds the connection open for up to 30s if no "
            "events are immediately available, polling internally "
            "for new events. Returns as soon as an event arrives or "
            "30s elapses (empty events list on timeout). Default is "
            "short-poll (immediate response)."
        ),
    ),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Pull events for an agent.

    Cursor-based pagination: pass ``since=<last next_cursor>`` on the
    next call. Server-side limit cap at 1000 (FastAPI ``le=1000``
    enforces).

    Two modes:

    * **Short-poll (default)** — immediate response. Caller polls at
      its own cadence (typically 2s from presence-runtime v0.2's
      ``EventConsumer``).
    * **Long-poll** (``wait=long``) — server holds the connection
      open up to 30s if no events are immediately available. Returns
      as soon as the next event arrives, or 30s elapses (empty
      events list, 200 status). Saves polling overhead at the cost
      of an open connection.

    **Implementation note**: long-poll uses a server-side internal
    poll loop (every ~1s within the 30s window). The original PR-1b
    design called for LISTEN/NOTIFY on a per-agent PostgreSQL channel;
    that's a future optimization (lower CPU + DB QPS at high
    subscriber counts) that swaps the implementation without changing
    the wire contract. Internal polling is sufficient for the v1
    consumer load + keeps the code simple + cleanly testable.

    Closes Q1 from the PR-1b spec (CTO concur 2026-05-11; deferred
    from PR-1b for clean-ship discipline; Backlog row
    ``cmp0jjz7c000004kzvphxkxf4``).
    """
    agent = await get_agent_owned(db, user, ref)

    # Short-poll path (default).
    events, next_cursor, has_more = await pull_events(
        db,
        recipient_agent_id=agent.id,
        since=since,
        limit=limit,
        event_type=event_type,
    )

    # Long-poll: if no events available + wait=long, delegate to the
    # extracted helper. Pulled out for direct testability + coverage
    # tracing reliability (see _run_long_poll_wait docstring).
    if wait == "long" and not events:
        events, next_cursor, has_more = await _run_long_poll_wait(
            db,
            recipient_agent_id=agent.id,
            since=since,
            limit=limit,
            event_type=event_type,
        )

    # Item 2(b) — cursor-advance-as-ack side-effect. Extracted to a
    # pure helper for direct testability + ASGI-coverage-tracing
    # reliability (per CLAUDE.md pure-helper extraction discipline;
    # same pattern as _run_long_poll_wait).
    await _advance_ack_after_pull(
        db,
        agent_id=agent.id,
        next_cursor=next_cursor,
        event_type=event_type,
    )

    return EventListResponse(
        events=[_event_to_response(e) for e in events],
        next_cursor=next_cursor,
        has_more=has_more,
    )
