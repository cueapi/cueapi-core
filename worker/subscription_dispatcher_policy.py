"""Per-tier policy gate for the subscription webhook dispatcher (Phase 4a).

Sits between ``_fetch_pending_events_for_sub`` and ``_deliver_webhook``
in ``worker/subscription_dispatcher.py``. Inspects each candidate event's
``payload.priority`` and applies tier-specific rules:

* **p=5 (urgent)** — passes through immediately. Unchanged v1 behavior.
* **p=4 (high)** — DEBOUNCED. At most one webhook fire per recipient
  per ``PRIORITY_4_DEBOUNCE_SECONDS`` window (default 2s). When a
  previous fire happened within the window, all p=4 events stay in
  the queue and re-evaluate next dispatch cycle.
* **p=3 (default)** — passes through. Unchanged v1 behavior.
* **p=2 + p=1 (low / archive)** — passes through at Phase 4a; Phase 4b
  swaps these to digest-batched emission. v1 behavior preserved
  until 4b ships so 4a alone is purely additive (debounce-only
  semantics).

Pure-helper design for ASGI-coverage-tracing reliability + direct
testability (per CLAUDE.md discipline). The dispatcher calls
:func:`apply_tier_policy` with the candidate events list + a Redis
client; the helper returns the events that should actually fire +
stamps the dispatched-at marker for p=4. The dispatcher's own
side-effects (HTTP POST, watermark advance) remain unchanged.

Why a separate module: keeps the dispatcher's existing surface small
(fetch → deliver → update watermark). Tier policy is a substantive
chunk of behavior that benefits from being unit-testable without
spinning up the whole dispatch loop. Pattern mirrors the
``_run_long_poll_wait`` extraction in PR #776.

Closes Phase 4a (Backlog ``cmp0qzg6l000004jr272gbirx``); spec at
https://trydock.ai/workspaces/handoff?surface=phase-4-priority-tier-dispatcher.
"""
from __future__ import annotations

import logging
import time
from typing import Any, List, Sequence, Tuple

from app.config import settings


logger = logging.getLogger(__name__)


# Priority constants — match the messaging primitive's 1-5 enum.
PRIORITY_URGENT = 5
PRIORITY_HIGH = 4
PRIORITY_DEFAULT = 3
PRIORITY_LOW = 2
PRIORITY_ARCHIVE = 1

# Redis key prefix for the p=4 debounce marker. One key per
# subscriber_agent_id; stores the last fire's Unix timestamp as a
# string. Expires after the debounce window so stale markers don't
# leak.
DEBOUNCE_KEY_PREFIX = "priority_4_debounce"


def _debounce_key(subscriber_agent_id: str) -> str:
    """Build the Redis key for a recipient's p=4 debounce marker."""
    return f"{DEBOUNCE_KEY_PREFIX}:{subscriber_agent_id}"


def _event_priority(event: Any) -> int:
    """Extract the priority field from an event's payload.

    Defaults to :data:`PRIORITY_DEFAULT` if the payload is missing
    the field or has a non-int value. Defensive — older event shapes
    pre-PR-2a may not have populated this column. Returning the
    default tier means they pass through unchanged (correct v1
    behavior for missing-metadata events).

    Accepts either an ORM ``Event`` instance with a ``.payload`` dict
    attribute or a plain dict (for direct-call unit tests).
    """
    payload = getattr(event, "payload", None)
    if payload is None and isinstance(event, dict):
        payload = event.get("payload") or event
    if not isinstance(payload, dict):
        return PRIORITY_DEFAULT
    value = payload.get("priority", PRIORITY_DEFAULT)
    if not isinstance(value, int):
        return PRIORITY_DEFAULT
    if value < PRIORITY_ARCHIVE or value > PRIORITY_URGENT:
        # Out-of-range priorities (shouldn't happen post-validation
        # at /v1/messages, but defensive) treated as default tier.
        return PRIORITY_DEFAULT
    return value


def _now_seconds() -> float:
    """Indirection for tests — monkeypatched to fake the clock."""
    return time.time()


async def _is_p4_debounced(redis, subscriber_agent_id: str) -> bool:
    """Check whether a recipient is currently inside the p=4 debounce
    window.

    Returns True if a fire happened within
    ``PRIORITY_4_DEBOUNCE_SECONDS`` of now. False otherwise (no
    recorded fire, or the recorded fire is older than the window).

    Redis-down is treated as "not debounced" (allow fire). The
    rationale matches the existing rate-limit-middleware fallback:
    don't let an infra outage on Redis silently suppress
    notifications. Logs a warning if Redis errors out so the
    operator notices.
    """
    try:
        raw = await redis.get(_debounce_key(subscriber_agent_id))
    except Exception as exc:  # noqa: BLE001 — Redis-down fallback
        logger.warning(
            "p=4 debounce Redis check failed; allowing fire",
            extra={
                "event_type": "p4_debounce_redis_error",
                "subscriber_agent_id": subscriber_agent_id,
                "error": str(exc)[:200],
            },
        )
        return False

    if raw is None:
        return False

    try:
        last_fire = float(raw)
    except (TypeError, ValueError):
        # Marker was malformed; treat as "no recent fire."
        return False

    elapsed = _now_seconds() - last_fire
    return elapsed < settings.PRIORITY_4_DEBOUNCE_SECONDS


async def _stamp_p4_fire(redis, subscriber_agent_id: str) -> None:
    """Record that a p=4 fire just happened for this recipient.

    Stores the current Unix timestamp + sets a TTL slightly longer
    than the debounce window so stale markers auto-expire. Redis-down
    is logged but not raised — the next fire will re-stamp.
    """
    key = _debounce_key(subscriber_agent_id)
    now_str = str(_now_seconds())
    # TTL = debounce window + 1s safety buffer. Slightly over so the
    # key is guaranteed to still be readable at the boundary.
    ttl_seconds = max(1, int(settings.PRIORITY_4_DEBOUNCE_SECONDS) + 1)
    try:
        await redis.set(key, now_str, ex=ttl_seconds)
    except Exception as exc:  # noqa: BLE001 — non-blocking
        logger.warning(
            "p=4 debounce stamp failed; next fire may re-fire within window",
            extra={
                "event_type": "p4_debounce_stamp_error",
                "subscriber_agent_id": subscriber_agent_id,
                "error": str(exc)[:200],
            },
        )


async def apply_tier_policy(
    redis,
    *,
    subscriber_agent_id: str,
    events: Sequence[Any],
) -> Tuple[List[Any], List[Any]]:
    """Apply per-tier dispatch policy to a candidate event batch.

    Returns ``(events_to_fire, events_deferred)``:

    * ``events_to_fire`` — pass through to the webhook POST. The
      dispatcher's existing flow handles them normally (watermark
      advances to ``events_to_fire[-1].id`` after successful fire).
    * ``events_deferred`` — held back this cycle. The dispatcher
      should NOT advance the watermark past these; they'll be
      re-evaluated next cycle.

    Phase 4a behavior (only p=4 has non-trivial policy):

    * p=5 / p=3 / p=2 / p=1 → events_to_fire (v1 behavior preserved
      for everything but p=4).
    * p=4 → events_to_fire ONLY if the recipient isn't currently in
      the debounce window. If they are, the p=4 events go to
      events_deferred.

    **Watermark math caveat** (callers MUST observe): when
    ``events_deferred`` is non-empty, the watermark must advance to
    the highest-id event in ``events_to_fire`` IF those events form
    a contiguous prefix of the original batch. If p=4 events are
    interleaved with higher-priority events, we'd lose ordering
    semantics by advancing past a deferred p=4. Phase 4a's solution:
    when ANY events are deferred, the dispatcher fires
    ``events_to_fire`` BUT advances watermark only to the lowest
    deferred event's ``id - 1``. The dispatcher implements this; the
    helper just returns the two lists.

    Note: the watermark-advance constraint is a sequencing decision
    documented at the call site, not enforced inside this helper.
    The helper's contract is purely "which events should fire now."
    """
    if not events:
        return [], []

    # Optimization: if no p=4 events in the batch, no debounce check
    # needed at all. Skip the Redis round trip in the common case.
    has_priority_4 = any(_event_priority(e) == PRIORITY_HIGH for e in events)
    if not has_priority_4:
        return list(events), []

    debounced = await _is_p4_debounced(redis, subscriber_agent_id)

    to_fire: List[Any] = []
    deferred: List[Any] = []

    for event in events:
        if _event_priority(event) == PRIORITY_HIGH and debounced:
            deferred.append(event)
        else:
            to_fire.append(event)

    return to_fire, deferred


async def stamp_dispatch_markers(
    redis,
    *,
    subscriber_agent_id: str,
    events: Sequence[Any],
) -> None:
    """Record dispatch markers for tiers that need post-fire state.

    Called AFTER ``_deliver_webhook`` succeeds. Currently only stamps
    the p=4 debounce marker (one per recipient regardless of how
    many p=4 events were in the batch — the marker is "did a fire
    happen recently?", not "how many events fired").

    Phase 4b will extend this with digest emission markers for
    p=1 + p=2. The function exists at v1 to keep the dispatcher's
    side-effect surface localized to one helper call.
    """
    if any(_event_priority(e) == PRIORITY_HIGH for e in events):
        await _stamp_p4_fire(redis, subscriber_agent_id)
