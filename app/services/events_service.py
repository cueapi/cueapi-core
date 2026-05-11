"""Event-emit primitive — service layer (PR-1b).

Pure-helper-friendly module: emit / pull / subscribe / list / detach.
HTTP routes (``app/routers/events.py``) are thin wrappers over these
functions. Webhook dispatch loop (``worker/poller.py``) reads
``Subscription`` rows + ``pull_events`` directly.

DORMANT shipping per PR-1b: nothing in the production call graph
hits ``emit_event`` yet. Messaging service (PR-2a) is the first
caller.

Per CTO 2026-05-11 locks:

* Event-type registry hardcoded for v0.1 (Q4 lock). Rejects unknown
  types at subscription-create time with a clear error. v0.2 may
  add agent-defined types if a real need surfaces.
* Authorization: agent-scoped. The caller of ``create_subscription``
  passes ``subscriber_agent_id`` resolved from the route-level auth
  check; this module trusts it. Never accept caller-supplied IDs.
* Idempotency on emit via UPSERT on ``(event_type, idempotency_key)``;
  re-emit returns the existing row id.
* Webhook secret minted server-side at create time. Returned ONCE
  in the response; not retrievable later (matches user-webhook
  rotate semantics).
* SSRF validation on ``webhook_url`` via the existing
  ``validate_callback_url`` helper.
"""
from __future__ import annotations

from typing import List, Literal, Optional, Tuple
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.event import Event
from app.models.subscription import Subscription
from app.utils.ids import generate_webhook_secret
from app.utils.url_validation import validate_callback_url


# ───────────────────────────────────────────────────────────────────────
# Event-type registry — hardcoded for v0.1 per CTO Q4 lock.
# ───────────────────────────────────────────────────────────────────────

KNOWN_EVENT_TYPES = frozenset({
    "message.delivered",
    "message.digest",
    "turn.pass",
})
"""Allow-list of event types subscribable at v0.1. PR-2a wires
``message.delivered`` emission from the messaging service. Phase 4b
adds ``message.digest`` — bundled summary of N low-priority (p=1/p=2)
events emitted periodically by the digest emitter. Item 2(a) (Backlog
``cmp1j1tt600040``, CTO ask 2026-05-11) adds ``turn.pass`` — zero-body
META envelope for inbox-watcher recipes to filter on by default
(consumer-side bundled in AttachSnippetGenerator v2.3, CMA's lane).
Future types (``message.read``, ``message.ack``,
``cue.execution.outcome``, ``agent.live_session.detached``) are out
of scope; adding them later is purely additive (new entries here)."""


# Server-side limits / defaults.

DEFAULT_PULL_LIMIT = 100
MAX_PULL_LIMIT = 1000
"""Server-side cap on ``pull_events(limit=...)``. Prevents a misbehaving
caller from pulling the entire events table in one request. Cursor
pagination via ``next_cursor`` is the supported way to walk many
events."""

INLINE_BODY_MAX_BYTES = 32 * 1024
"""32KB cap for inline body embedding (Item 1 Option 1, CTO concur
2026-05-11). Bodies ≤ cap → embedded as ``payload.body``. Bodies
> cap → omitted; ``payload.body_omitted = "size_too_large"`` +
``payload.body_size_bytes = <N>`` signal the consumer to fetch the
full body via GET /v1/messages/{id}. Empirical justification:
99.94% of cue payloads on staging ≤ 65 bytes; Slack-style P99
≈ 10KB — 32KB has comfortable margin."""

WEBHOOK_CIRCUIT_BREAKER_THRESHOLD = 10
"""``consecutive_failures`` ceiling — the webhook dispatch loop sets
``paused_until = NOW() + 1h`` once a sub crosses this. Pull surface
unaffected."""


# ───────────────────────────────────────────────────────────────────────
# Typed errors — service-layer signals that routes translate to HTTP.
# ───────────────────────────────────────────────────────────────────────

class EventsServiceError(Exception):
    """Base class for service-layer errors. Routes pattern-match on
    subclasses to choose the HTTP status / error code."""

    code: str = "internal_error"
    status: int = 500


class UnknownEventTypeError(EventsServiceError):
    """Raised when create_subscription is called with an event_type
    not in :data:`KNOWN_EVENT_TYPES`. Routes return 400."""

    code = "unknown_event_type"
    status = 400


class InvalidDeliveryTargetError(EventsServiceError):
    """Raised when delivery_target / webhook_url combination is
    invalid (pull with url, or webhook without url). Routes
    return 400."""

    code = "invalid_delivery_target"
    status = 400


class InvalidWebhookUrlError(EventsServiceError):
    """Raised when webhook_url fails SSRF validation. Routes
    return 400."""

    code = "invalid_webhook_url"
    status = 400


class SubscriptionNotFoundError(EventsServiceError):
    """Raised by detach when no active subscription matches the
    id + subscriber_agent_id. Routes return 404."""

    code = "subscription_not_found"
    status = 404


# ───────────────────────────────────────────────────────────────────────
# Emit — append-only with idempotency.
# ───────────────────────────────────────────────────────────────────────

async def _maybe_embed_body(
    db: AsyncSession,
    *,
    recipient_agent_id: str,
    event_type: str,
    body_text: str,
    payload: dict,
) -> dict:
    """Helper: decide whether to embed body_text into payload.

    Pure-helper-friendly: checks if ANY active subscription for
    (recipient, event_type) has inline_body=True. If yes:

    - Body ≤ INLINE_BODY_MAX_BYTES → embed as ``payload['body']``
    - Body > INLINE_BODY_MAX_BYTES → set ``payload['body_omitted']
      = 'size_too_large'`` + ``payload['body_size_bytes'] = N``

    If no active inline_body subscription exists, returns payload
    unchanged (META-only emit, the existing behavior).

    Item 1 Option 1 (Backlog cmp1j1rzs00020) — coexists architecturally
    with CMA's Option 2 (runtime-side body-detect + skip-fetch).
    Both ship per CTO direction 2026-05-11.
    """
    # Cheap pre-check: is there at least one active inline_body
    # subscription for this recipient + event_type? Single SELECT;
    # uses ``ux_subscriptions_active_unique`` partial index.
    from app.models.subscription import Subscription

    stmt = (
        select(Subscription.id)
        .where(Subscription.subscriber_agent_id == recipient_agent_id)
        .where(Subscription.event_type == event_type)
        .where(Subscription.detached_at.is_(None))
        .where(Subscription.inline_body.is_(True))
        .limit(1)
    )
    has_inline_sub = (await db.execute(stmt)).scalar_one_or_none() is not None

    if not has_inline_sub:
        return payload

    body_bytes = len(body_text.encode("utf-8"))
    enriched = dict(payload)  # don't mutate caller's dict
    if body_bytes <= INLINE_BODY_MAX_BYTES:
        enriched["body"] = body_text
    else:
        enriched["body_omitted"] = "size_too_large"
        enriched["body_size_bytes"] = body_bytes
    return enriched


async def emit_event(
    db: AsyncSession,
    *,
    event_type: str,
    recipient_agent_id: str,
    payload: dict,
    idempotency_key: Optional[str] = None,
    body_text: Optional[str] = None,
) -> Event:
    """Append a new event row for a recipient.

    Idempotency: if ``idempotency_key`` is provided and an event with
    the same ``(event_type, idempotency_key)`` already exists, returns
    the existing row instead of creating a duplicate. Matches the
    ``dispatch_outbox`` idempotency pattern.

    Returns the persisted :class:`Event`. Caller is responsible for
    transaction commit (the route / worker dispatcher manages that).

    **Trust contract**: validation of ``event_type`` against the
    registry happens at subscription-create time, NOT here. Emit is
    on the hot path; the messaging service emits trusted types only.
    Validating here would gate every fire on a registry check.

    **Item 1 inline_body** (CTO concur 2026-05-11): if ``body_text``
    is provided AND the recipient has an active subscription with
    ``inline_body=True`` for this ``event_type``, the body is
    embedded in ``payload.body`` (≤32KB) or omit-flagged (>32KB).
    Callers that don't have a body to embed pass ``body_text=None``
    (the default) and the function skips the subscription lookup —
    zero perf cost for non-body emit paths.
    """
    # Item 1 — body embedding decision. Pure-helper-friendly via
    # ``_maybe_embed_body``; called only when ``body_text`` is
    # provided so non-body emit paths skip the SQL.
    if body_text is not None:
        payload = await _maybe_embed_body(
            db,
            recipient_agent_id=recipient_agent_id,
            event_type=event_type,
            body_text=body_text,
            payload=payload,
        )

    if idempotency_key is None:
        # Plain INSERT — no dedup needed.
        event = Event(
            event_type=event_type,
            recipient_agent_id=recipient_agent_id,
            payload=payload or {},
        )
        db.add(event)
        await db.flush()
        await db.refresh(event)
        return event

    # UPSERT via Postgres ON CONFLICT. The partial-unique index
    # ``ux_events_idempotency_key`` on (event_type, idempotency_key)
    # WHERE idempotency_key IS NOT NULL is the conflict target.
    # On conflict, return the existing row by setting id=id (a no-op
    # update that triggers RETURNING).
    stmt = (
        pg_insert(Event)
        .values(
            event_type=event_type,
            recipient_agent_id=recipient_agent_id,
            payload=payload or {},
            idempotency_key=idempotency_key,
        )
        .on_conflict_do_update(
            index_elements=[Event.event_type, Event.idempotency_key],
            index_where=Event.idempotency_key.is_not(None),
            set_={"event_type": Event.event_type},  # no-op set; triggers RETURNING
        )
        .returning(Event)
    )
    result = await db.execute(stmt)
    row = result.scalar_one()
    return row


# ───────────────────────────────────────────────────────────────────────
# Pull — cursor-paginated read.
# ───────────────────────────────────────────────────────────────────────

async def pull_events(
    db: AsyncSession,
    *,
    recipient_agent_id: str,
    since: int = 0,
    limit: int = DEFAULT_PULL_LIMIT,
    event_type: Optional[str] = None,
) -> Tuple[List[Event], Optional[int], bool]:
    """Pull events for a recipient with cursor-based pagination.

    Reads via ``ix_events_recipient_id_cursor`` (composite btree on
    ``recipient_agent_id, id``). Filter: ``id > since`` ordered ASC.

    Returns ``(events, next_cursor, has_more)``:

    * ``events`` — list of :class:`Event` rows, length <= ``limit``
    * ``next_cursor`` — ``id`` of the last event returned, or ``None``
      if the list is empty
    * ``has_more`` — ``True`` if the page hit ``limit`` (caller should
      re-poll with ``since=next_cursor`` immediately)

    Pull-mode resume = caller persists ``next_cursor`` locally and
    passes it as ``since`` on next call. Stateless from server's POV.
    """
    # Clamp limit at server-side cap.
    effective_limit = min(max(limit, 1), MAX_PULL_LIMIT)

    stmt = (
        select(Event)
        .where(Event.recipient_agent_id == recipient_agent_id)
        .where(Event.id > since)
        .order_by(Event.id.asc())
        .limit(effective_limit)
    )
    if event_type is not None:
        stmt = stmt.where(Event.event_type == event_type)

    result = await db.execute(stmt)
    events = list(result.scalars().all())

    next_cursor = events[-1].id if events else None
    has_more = len(events) == effective_limit
    return events, next_cursor, has_more


# ───────────────────────────────────────────────────────────────────────
# Subscriptions — create / list / detach.
# ───────────────────────────────────────────────────────────────────────

def _validate_event_type(event_type: str) -> None:
    if event_type not in KNOWN_EVENT_TYPES:
        raise UnknownEventTypeError(
            f"event_type {event_type!r} not in v0.1 registry. "
            f"Known types: {sorted(KNOWN_EVENT_TYPES)}"
        )


def _validate_delivery_target_combo(
    delivery_target: str, webhook_url: Optional[str]
) -> None:
    """The (target, url) pair must match one of two shapes:

    * ``target='pull'`` + ``url=None``
    * ``target='webhook'`` + ``url=<non-empty str>``

    Mirrors the DB CHECK constraint so the API returns a clean 400
    instead of bubbling a 500 from the constraint violation.
    """
    if delivery_target == "pull":
        if webhook_url is not None:
            raise InvalidDeliveryTargetError(
                "delivery_target='pull' must not include webhook_url"
            )
    elif delivery_target == "webhook":
        if not webhook_url:
            raise InvalidDeliveryTargetError(
                "delivery_target='webhook' requires webhook_url"
            )
    else:
        raise InvalidDeliveryTargetError(
            f"delivery_target must be 'pull' or 'webhook', got {delivery_target!r}"
        )


async def create_subscription(
    db: AsyncSession,
    *,
    subscriber_agent_id: str,
    event_type: str,
    delivery_target: Literal["pull", "webhook"],
    webhook_url: Optional[str] = None,
    inline_body: bool = False,
) -> Subscription:
    """Create a subscription row for an agent.

    **Trust contract**: ``subscriber_agent_id`` is stamped by the
    route after resolving ``{ref}`` to an agent owned by the
    authenticated user. This function trusts it; never let the
    caller supply it directly.

    Validations:

    * ``event_type`` must be in :data:`KNOWN_EVENT_TYPES` (registry)
    * ``(delivery_target, webhook_url)`` must match one of the two
      valid shapes (pull-no-url / webhook-with-url)
    * If ``delivery_target='webhook'``, ``webhook_url`` must pass
      SSRF validation (existing ``validate_callback_url`` helper)

    Mints ``webhook_secret`` server-side at create time for webhook
    subs. Returns the persisted :class:`Subscription`. The route
    layer surfaces ``webhook_secret`` to the caller ONCE in the
    create response; subsequent reads do not expose it (matches
    user-webhook rotate semantics).
    """
    _validate_event_type(event_type)
    _validate_delivery_target_combo(delivery_target, webhook_url)

    webhook_secret: Optional[str] = None
    if delivery_target == "webhook":
        # SSRF check. Use existing helper so all callback-url
        # validations share one allowlist + one set of edge cases.
        is_valid, error = validate_callback_url(
            webhook_url,  # type: ignore[arg-type]  # asserted non-None above
            env=getattr(settings, "ENV", "production"),
        )
        if not is_valid:
            raise InvalidWebhookUrlError(error)
        webhook_secret = generate_webhook_secret()

    sub = Subscription(
        subscriber_agent_id=subscriber_agent_id,
        event_type=event_type,
        delivery_target=delivery_target,
        webhook_url=webhook_url,
        webhook_secret=webhook_secret,
        inline_body=inline_body,
    )
    db.add(sub)
    await db.flush()
    await db.refresh(sub)
    return sub


async def list_subscriptions(
    db: AsyncSession,
    *,
    subscriber_agent_id: str,
) -> List[Subscription]:
    """List active (non-detached) subscriptions for an agent.

    Returns all fields including dispatch-state surface
    (``last_dispatched_event_id``, ``last_dispatched_at``,
    ``consecutive_failures``, ``paused_until``) per CTO correction #2
    so recipients can observe their own paused-webhook state. Route
    layer redacts ``webhook_url`` to host-only before responding +
    omits ``webhook_secret`` entirely (only revealed once at create).
    """
    stmt = (
        select(Subscription)
        .where(Subscription.subscriber_agent_id == subscriber_agent_id)
        .where(Subscription.detached_at.is_(None))
        .order_by(Subscription.created_at.asc())
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def detach_subscription(
    db: AsyncSession,
    *,
    subscription_id: UUID,
    subscriber_agent_id: str,
) -> bool:
    """Soft-detach a subscription.

    Sets ``detached_at = NOW()`` on the row IF it belongs to the
    given agent AND is currently active (``detached_at IS NULL``).
    Returns ``True`` if a row was updated, ``False`` if no matching
    active row exists (could be already-detached or wrong-owner).

    **Idempotency**: re-DELETE returns ``False`` but the route layer
    still responds 200 — repeated detach is not an error from the
    caller's POV. To distinguish wrong-owner from already-detached,
    routes can issue a follow-up SELECT, but the v0.1 contract
    accepts both as the same observable outcome (detached).
    """
    stmt = (
        update(Subscription)
        .where(Subscription.id == subscription_id)
        .where(Subscription.subscriber_agent_id == subscriber_agent_id)
        .where(Subscription.detached_at.is_(None))
        .values(detached_at=func.now())
    )
    result = await db.execute(stmt)
    return (result.rowcount or 0) > 0


# ───────────────────────────────────────────────────────────────────────
# Item 2(b) — cursor-advance-as-ack semantic
# ───────────────────────────────────────────────────────────────────────


async def advance_ack_watermark(
    db: AsyncSession,
    *,
    recipient_agent_id: str,
    new_acked_event_id: int,
    event_type: Optional[str] = None,
) -> int:
    """Advance ``last_acked_event_id`` for matching pull subscriptions.

    Called by ``GET /v1/agents/{ref}/events`` after returning events
    — the cursor's progression IS the ack signal. Only updates rows
    where the new id is strictly greater than the current watermark
    (never moves backwards).

    Scope:

    * **Pull-mode only** at this entry point. Webhook subs advance
      their ack watermark inside the dispatch loop (alongside
      ``last_dispatched_event_id``), not via this helper.
    * **event_type filter** if provided — caller can scope to a
      specific event_type (matches the GET /events ``?event_type=X``
      query param). If None, advances all matching pull subs for the
      recipient.

    Returns the number of rows updated. Pure side-effect; the caller
    has already returned the events to the consumer at this point.

    Item 2(b) (Backlog cmp1j1vlp00060). Resolves CTO concur shape:
    consumer's natural pull cadence advances ack; explicit PATCH
    path is the secondary surface for webhook + no-pull callers.
    """
    stmt = (
        update(Subscription)
        .where(Subscription.subscriber_agent_id == recipient_agent_id)
        .where(Subscription.delivery_target == "pull")
        .where(Subscription.detached_at.is_(None))
        .where(
            # Only advance forward. NULL watermark is "never acked";
            # we treat any new_acked_event_id > 0 as forward of NULL.
            (Subscription.last_acked_event_id.is_(None))
            | (Subscription.last_acked_event_id < new_acked_event_id)
        )
        .values(last_acked_event_id=new_acked_event_id)
    )
    if event_type is not None:
        stmt = stmt.where(Subscription.event_type == event_type)
    result = await db.execute(stmt)
    return result.rowcount or 0


async def ack_subscription(
    db: AsyncSession,
    *,
    subscription_id: UUID,
    subscriber_agent_id: str,
    acked_event_id: int,
) -> bool:
    """Explicit ack — PATCH /v1/agents/{ref}/subscriptions/{id}/ack.

    For webhook consumers + pull consumers wanting to ack without
    pulling new events. Updates the given subscription's
    ``last_acked_event_id`` if (a) the row is owned by the given
    agent and (b) the new value is strictly greater than current.

    Returns ``True`` if the row was updated. ``False`` if the row
    is owned by another agent, detached, or the new value is not
    strictly greater (no-op idempotent).

    Watermark monotonicity is enforced server-side: callers can't
    accidentally rewind a subscription's ack to an earlier event.
    """
    stmt = (
        update(Subscription)
        .where(Subscription.id == subscription_id)
        .where(Subscription.subscriber_agent_id == subscriber_agent_id)
        .where(Subscription.detached_at.is_(None))
        .where(
            (Subscription.last_acked_event_id.is_(None))
            | (Subscription.last_acked_event_id < acked_event_id)
        )
        .values(last_acked_event_id=acked_event_id)
    )
    result = await db.execute(stmt)
    return (result.rowcount or 0) > 0
