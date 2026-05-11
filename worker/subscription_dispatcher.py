"""Webhook dispatch loop for the event-emit primitive (PR-1b).

Lives in ``worker/`` alongside ``poller.py`` so the existing leader-
election + run_poller cycle wraps it. Two surfaces:

* ``dispatch_subscription_events(db_engine, batch_size)`` — polled
  every cycle; finds webhook subs with pending events + POSTs them
  with HMAC signature. Circuit-breaker at 10 consecutive failures
  → ``paused_until = NOW() + 1h``.
* ``cleanup_old_events(db_engine, retention_days=7)`` — hourly cron;
  deletes events older than the retention window. Matches the
  cleanup-outbox pattern.

Both ship DORMANT in PR-1b: the loop runs but, with zero
subscriptions in production until PR-2a starts emitting, every
cycle finds nothing. Zero behavior change. Once PR-2a wires
emission + customers create subscriptions, the loop becomes active
without any further deploy.

Separate file from ``poller.py`` rather than ballooning that
already-1500-line module. ``run_poller`` calls these as a two-line
addition; the leader-election + heartbeat surface is unchanged.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import httpx
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.models.event import Event
from app.models.subscription import Subscription
from app.utils.signing import sign_payload
from worker.subscription_dispatcher_policy import (
    apply_tier_policy,
    stamp_dispatch_markers,
)


logger = logging.getLogger(__name__)


# Tunables — exposed as constants so tests can monkeypatch.

DISPATCH_BATCH_SIZE = 100
"""Max subscriptions processed per dispatch cycle. Each sub may
emit multiple events in its POST batch."""

DISPATCH_EVENTS_PER_SUB = 50
"""Max events sent in one webhook POST. Bounded so a recipient
with a large backlog doesn't get an unreasonably-large request
body."""

DISPATCH_HTTP_TIMEOUT_SECONDS = 30.0

CIRCUIT_BREAKER_THRESHOLD = 10
"""Consecutive-failure ceiling. Crossing this triggers a 1h pause."""

CIRCUIT_BREAKER_PAUSE_SECONDS = 3600
"""How long ``paused_until`` extends past NOW when the breaker
trips."""


# ───────────────────────────────────────────────────────────────────────
# Pure helpers — unit-testable without DB or HTTP.
# ───────────────────────────────────────────────────────────────────────

def _serialize_event(event: Event) -> Dict[str, Any]:
    """Stable wire shape for an event row inside a webhook batch."""
    return {
        "id": event.id,
        "event_type": event.event_type,
        "payload": event.payload or {},
        "emitted_at": event.emitted_at.isoformat() if event.emitted_at else None,
    }


def _build_webhook_body(
    subscription_id: str, events: List[Event]
) -> Dict[str, Any]:
    """Compose the JSON body posted to the subscriber's webhook URL.

    Shape matches PR-1b spec §Webhook payload shape:
    ``{delivery_id, subscription_id, events: [...]}``. ``delivery_id``
    is derived from the first + last event ids; not a separate
    sequence — keeping it deterministic per (sub, event-range) makes
    idempotent retries on the recipient side trivial.
    """
    first_id = events[0].id if events else 0
    last_id = events[-1].id if events else 0
    return {
        "delivery_id": f"dlv_{subscription_id}_{first_id}_{last_id}",
        "subscription_id": subscription_id,
        "events": [_serialize_event(e) for e in events],
    }


def _should_trip_breaker(consecutive_failures: int) -> bool:
    """The threshold check, factored for clarity + dedicated test
    coverage on the boundary case."""
    return consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD


def _classify_response(
    *, ok: bool, status_code: int
) -> str:
    """Classify a webhook response into a dispatch outcome string.

    Returns one of:

    * ``"success"`` — 2xx; advance watermark, reset failures.
    * ``"retry"`` — 5xx / 408 / 429 / network error (status=0);
      bump failures, do NOT advance (same events retried next loop).
    * ``"skip"`` — 4xx other than 408/429 with a real status; bump
      failures, do NOT advance (caller-side issue; events will keep
      failing until caller fixes their webhook or the breaker trips).

    408 + 429 map to retry semantics (per spec §Failure handling).
    Network errors surface as ``status_code=0`` from ``_deliver_webhook``;
    treated as retry since they're typically transient.
    """
    if ok:
        return "success"
    if status_code == 0 or status_code >= 500 or status_code in (408, 429):
        return "retry"
    return "skip"


# ───────────────────────────────────────────────────────────────────────
# Dispatch loop
# ───────────────────────────────────────────────────────────────────────

async def _fetch_dispatch_due_subs(
    conn,
    batch_size: int,
) -> List[Subscription]:
    """Pick active webhook subs that aren't paused. The
    ix_subscriptions_dispatch_due index covers most of the
    selectivity; the paused_until filter is at query time per the
    NOW()-IMMUTABLE partial-predicate gotcha."""
    now = datetime.now(timezone.utc)
    stmt = (
        select(Subscription)
        .where(Subscription.delivery_target == "webhook")
        .where(Subscription.detached_at.is_(None))
        .where(
            (Subscription.paused_until.is_(None))
            | (Subscription.paused_until < now)
        )
        .order_by(Subscription.last_dispatched_event_id.asc().nullsfirst())
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )
    result = await conn.execute(stmt)
    return list(result.scalars().all())


async def _fetch_pending_events_for_sub(
    conn,
    sub: Subscription,
) -> List[Event]:
    """Pull events for the subscription that haven't been dispatched.

    Filters by ``event_type`` (subs are per-type) and ``id >
    last_dispatched_event_id``. Uses ix_events_recipient_id_cursor
    for the recipient scan; ascends by id for stable order.
    """
    since = sub.last_dispatched_event_id or 0
    stmt = (
        select(Event)
        .where(Event.recipient_agent_id == sub.subscriber_agent_id)
        .where(Event.event_type == sub.event_type)
        .where(Event.id > since)
        .order_by(Event.id.asc())
        .limit(DISPATCH_EVENTS_PER_SUB)
    )
    result = await conn.execute(stmt)
    return list(result.scalars().all())


async def _deliver_webhook(
    *,
    url: str,
    secret: str,
    body: Dict[str, Any],
) -> tuple[bool, int]:
    """POST to the subscriber's webhook URL with HMAC signature.

    Returns ``(ok, http_status)``. ``ok=True`` only on 2xx. Network
    errors map to ``(False, 0)``.

    Signature header format matches existing CueAPI webhook signing:
    ``X-CueAPI-Signature: v1=<hex_digest>`` over ``{timestamp}.{json_body}``.
    """
    # sign_payload expects a dict and does its own JSON serialization
    # with sorted keys; the recipient reproduces the same bytes by
    # JSON-serializing the body they receive with sorted keys.
    timestamp, signature = sign_payload(body, secret)
    payload_bytes = json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-CueAPI-Signature": signature,
        "X-CueAPI-Timestamp": str(timestamp),
        "X-CueAPI-Event-Count": str(len(body.get("events", []))),
    }
    try:
        async with httpx.AsyncClient(
            timeout=DISPATCH_HTTP_TIMEOUT_SECONDS,
            follow_redirects=False,
        ) as client:
            resp = await client.post(url, content=payload_bytes, headers=headers)
        return (200 <= resp.status_code < 300, resp.status_code)
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        logger.warning(
            "subscription webhook delivery network error",
            extra={
                "event_type": "subscription_dispatch_network_error",
                "url": url,
                "error": str(exc)[:200],
            },
        )
        return (False, 0)


async def dispatch_subscription_events(
    db_engine: AsyncEngine,
    batch_size: int = DISPATCH_BATCH_SIZE,
    redis=None,
) -> int:
    """Drain one cycle of pending webhook deliveries.

    Returns the number of subscriptions that received delivery
    attempts (success + failure). Idempotent at the subscription
    level — running back-to-back is safe.

    The leader-election guard in ``run_poller`` ensures only one
    replica runs this per cycle; ``with_for_update(skip_locked=True)``
    inside the helper is belt-and-suspenders for the rare case where
    two replicas briefly contend the leader lock.

    Uses an ORM session (rather than a raw Connection) so the
    helpers can return ``Subscription`` / ``Event`` instances and
    ``sub.consecutive_failures + 1`` etc. evaluates against ORM
    attributes instead of raw Row tuples.
    """
    attempts = 0
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async with session_factory() as session:
        async with session.begin():
            subs = await _fetch_dispatch_due_subs(session, batch_size)

            for sub in subs:
                events = await _fetch_pending_events_for_sub(session, sub)
                if not events:
                    # Sub is active but no pending events — common
                    # idle case. Skip silently.
                    continue

                # Phase 4a — per-tier policy. Filters p=4 events when
                # the recipient is in the debounce window; passes the
                # rest through. Redis-down falls through to "fire
                # everything" per the helper's defensive behavior.
                if redis is not None:
                    events_to_fire, events_deferred = await apply_tier_policy(
                        redis,
                        subscriber_agent_id=sub.subscriber_agent_id,
                        events=events,
                    )
                else:
                    events_to_fire, events_deferred = list(events), []

                if not events_to_fire:
                    # All events deferred (only p=4 in the batch +
                    # recipient is debounced). Skip the webhook fire;
                    # leave watermark unadvanced; re-evaluate next
                    # cycle. NOT counted as an attempt.
                    continue

                body = _build_webhook_body(str(sub.id), events_to_fire)
                ok, status_code = await _deliver_webhook(
                    url=sub.webhook_url,  # type: ignore[arg-type]  # delivery_target='webhook' guarantees non-None
                    secret=sub.webhook_secret,  # type: ignore[arg-type]
                    body=body,
                )
                outcome = _classify_response(ok=ok, status_code=status_code)

                if outcome == "success":
                    # Phase 4a watermark math: when some events are
                    # deferred, advance only up to the highest-id
                    # event in events_to_fire that's BEFORE the lowest
                    # deferred event. Preserves ordering: deferred
                    # events stay in the queue at their original id
                    # for the next cycle. With no deferred events,
                    # advances to events_to_fire[-1].id (v1 behavior).
                    if events_deferred:
                        lowest_deferred_id = min(e.id for e in events_deferred)
                        contiguous_fired = [
                            e for e in events_to_fire if e.id < lowest_deferred_id
                        ]
                        new_watermark = (
                            contiguous_fired[-1].id
                            if contiguous_fired
                            else (sub.last_dispatched_event_id or 0)
                        )
                    else:
                        new_watermark = events_to_fire[-1].id

                    await session.execute(
                        update(Subscription)
                        .where(Subscription.id == sub.id)
                        .values(
                            last_dispatched_event_id=new_watermark,
                            last_dispatched_at=datetime.now(timezone.utc),
                            consecutive_failures=0,
                        )
                    )

                    # Phase 4a — record p=4 fire so subsequent cycles
                    # within the window suppress further p=4 fires
                    # to the same recipient.
                    if redis is not None:
                        await stamp_dispatch_markers(
                            redis,
                            subscriber_agent_id=sub.subscriber_agent_id,
                            events=events_to_fire,
                        )
                else:
                    # retry + skip both bump the failure counter without
                    # advancing the watermark. Distinction matters for
                    # logging + future retry-policy tuning but not for
                    # watermark math.
                    new_failures = sub.consecutive_failures + 1
                    paused_until = sub.paused_until
                    if _should_trip_breaker(new_failures):
                        paused_until = datetime.now(timezone.utc) + timedelta(
                            seconds=CIRCUIT_BREAKER_PAUSE_SECONDS
                        )
                        logger.warning(
                            "subscription webhook circuit breaker tripped",
                            extra={
                                "event_type": "subscription_circuit_breaker_tripped",
                                "subscription_id": str(sub.id),
                                "consecutive_failures": new_failures,
                                "paused_until": paused_until.isoformat(),
                            },
                        )
                    await session.execute(
                        update(Subscription)
                        .where(Subscription.id == sub.id)
                        .values(
                            consecutive_failures=new_failures,
                            paused_until=paused_until,
                        )
                    )
                attempts += 1

    return attempts


# ───────────────────────────────────────────────────────────────────────
# Cleanup — 1h cron task, deletes events older than retention.
# ───────────────────────────────────────────────────────────────────────

async def cleanup_old_events(
    db_engine: AsyncEngine,
    retention_days: int = 7,
) -> int:
    """Delete events older than ``retention_days``. Returns rowcount.

    Matches ``cleanup_outbox`` pattern. Uses ``ix_events_emitted_at``
    for the range scan. Called from ``run_poller``'s hourly cleanup
    guard (the ``_last_cleanup`` timestamp).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    async with db_engine.begin() as conn:
        result = await conn.execute(
            delete(Event).where(Event.emitted_at < cutoff)
        )
        deleted = result.rowcount or 0
    if deleted > 0:
        logger.info(
            "events cleanup complete",
            extra={
                "event_type": "events_cleanup",
                "deleted_count": deleted,
                "retention_days": retention_days,
            },
        )
    return deleted
