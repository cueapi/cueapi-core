"""Phase 4b — periodic digest emitter for low-priority subscription events.

Runs every ``DIGEST_PERIOD_SECONDS`` (default 600s = 10min) from
``run_poller``. For each recipient with un-digested ``message.delivered``
events of priority 1 or 2 in the events table:

1. Fetch all un-digested low-priority events (single SQL query).
2. Bundle them into a single ``message.digest`` event with a
   preview-only summary per CTO concur 2026-05-11 (full bodies are
   pulled via ``/v1/messages/{id}`` if the recipient wants them).
3. Mark source events ``digested_at = NOW()`` so the next cycle
   doesn't re-bundle them.

The new digest event flows through the existing subscription dispatch
loop normally — a subscriber configured for ``message.digest`` gets
the bundled payload via webhook or pull.

**Phase 4b scope** (per CTO ship-order concur):

- Add ``digested_at`` column to events table (migration 060) — done
  in the same PR as this module.
- Periodic emitter (this module) — wired into ``run_poller`` with
  a ``_last_digest_emit`` timestamp guard, similar to the existing
  hourly ``_last_cleanup`` pattern.
- New event type ``message.digest`` registered in
  ``KNOWN_EVENT_TYPES``.

**What ships dormant at Phase 4b:**

- Without a subscriber configured for ``message.digest``, the
  emitter still runs (marks p=1/p=2 events as digested) but no
  downstream consumer sees the bundle. Phase 4b is the producer
  side; consumer-side adoption is per-subscriber opt-in.

Closes second sub-step of Backlog row ``cmp0qzg6l000004jr272gbirx``.
Pure-helper extraction throughout for testability + ASGI-coverage
tracing reliability.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.config import settings
from app.models.event import Event
from app.services.events_service import emit_event


logger = logging.getLogger(__name__)


# Priority values eligible for digest batching. Matches Phase 4a's
# constants but not imported from there to avoid a circular dep —
# the values are an interface, not a dependency.
DIGEST_ELIGIBLE_PRIORITIES = (1, 2)


def _build_digest_payload(
    *,
    recipient_agent_id: str,
    bundled_events: List[Event],
    period_start: datetime,
    period_end: datetime,
) -> Dict[str, Any]:
    """Compose the wire-shape payload for a ``message.digest`` event.

    Per CTO concur 2026-05-11 — preview-only, NOT full body. The
    payload carries enough context for the recipient to decide
    whether to fetch full bodies via ``/v1/messages/{id}``.

    Pure function — no I/O. Directly unit-testable.
    """
    bundled_messages: List[Dict[str, Any]] = []
    for ev in bundled_events:
        payload = ev.payload or {}
        bundled_messages.append({
            "message_id": payload.get("message_id"),
            "sender_agent_id": payload.get("sender_agent_id"),
            "subject": payload.get("subject"),
            "priority": payload.get("priority"),
            "emitted_at": ev.emitted_at.isoformat() if ev.emitted_at else None,
        })

    return {
        "recipient_agent_id": recipient_agent_id,
        "digest_period_start": period_start.isoformat(),
        "digest_period_end": period_end.isoformat(),
        "bundle_count": len(bundled_events),
        "bundled_messages": bundled_messages,
    }


async def _fetch_undigested_events_grouped_by_recipient(
    session,
) -> Dict[str, List[Event]]:
    """Pull all un-digested low-priority events grouped by recipient.

    Returns a dict mapping recipient_agent_id → list of events. The
    digest emitter iterates this dict + emits one digest per
    recipient. Single SQL query keeps the lock window minimal.

    Filter:
    - event_type = 'message.delivered'
    - digested_at IS NULL
    - payload->>'priority' IN ('1', '2')

    Uses the ``ix_events_undigested`` partial index (migration 060)
    for the digested_at IS NULL filter; the priority filter is at
    query time against the JSONB payload.
    """
    stmt = (
        select(Event)
        .where(Event.event_type == "message.delivered")
        .where(Event.digested_at.is_(None))
        .where(
            Event.payload["priority"].as_string().in_(["1", "2"])
        )
        .order_by(Event.recipient_agent_id, Event.id)
    )
    result = await session.execute(stmt)
    rows = result.scalars().all()

    grouped: Dict[str, List[Event]] = {}
    for ev in rows:
        grouped.setdefault(ev.recipient_agent_id, []).append(ev)
    return grouped


async def emit_digests(db_engine: AsyncEngine) -> int:
    """Drain one digest cycle. Returns the number of digest events emitted.

    For each recipient with un-digested low-priority events:

    1. Fetch their un-digested events (via grouped query above).
    2. Skip if bundle count < ``DIGEST_MIN_BATCH_SIZE`` (default 1).
    3. Compute period_start = oldest event.emitted_at, period_end = NOW().
    4. Emit a single ``message.digest`` event via
       :func:`emit_event` with idempotency_key derived from
       (recipient + highest event_id) so a duplicate cycle within
       the same period is a no-op.
    5. Mark all source events ``digested_at = NOW()``.

    Idempotency-key shape: ``message.digest:{recipient_agent_id}:{highest_event_id}``.
    This ensures retrying the same emit (e.g., transient DB blip mid-
    cycle) doesn't produce duplicate digest rows. The
    ``ux_events_idempotency_key`` partial-unique index in migration 058
    enforces this server-side.

    Failure handling: if marking source events as digested fails
    after the digest event was emitted, the digest event still exists
    + carries the right data; the next cycle would attempt to re-emit
    with the same idempotency_key (returning the existing row). The
    next cycle WILL re-mark source events as digested. Failure mode
    is "digest event emitted twice, source events eventually
    marked" — both states are recoverable.
    """
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    digests_emitted = 0
    now = datetime.now(timezone.utc)

    async with session_factory() as session:
        async with session.begin():
            grouped = await _fetch_undigested_events_grouped_by_recipient(session)

            for recipient_agent_id, events in grouped.items():
                if len(events) < settings.DIGEST_MIN_BATCH_SIZE:
                    # Below threshold — skip this cycle; next cycle
                    # re-evaluates. Events stay un-digested.
                    continue

                period_start = events[0].emitted_at or now
                period_end = now
                highest_event_id = events[-1].id

                payload = _build_digest_payload(
                    recipient_agent_id=recipient_agent_id,
                    bundled_events=events,
                    period_start=period_start,
                    period_end=period_end,
                )

                await emit_event(
                    session,
                    event_type="message.digest",
                    recipient_agent_id=recipient_agent_id,
                    payload=payload,
                    idempotency_key=f"message.digest:{recipient_agent_id}:{highest_event_id}",
                )

                # Mark source events as digested. Single UPDATE
                # statement filtered by event_id so it's O(1) per
                # event regardless of recipient count.
                source_ids = [ev.id for ev in events]
                await session.execute(
                    update(Event)
                    .where(Event.id.in_(source_ids))
                    .values(digested_at=func.now())
                )

                digests_emitted += 1
                logger.info(
                    "digest emitted",
                    extra={
                        "event_type": "digest_emitted",
                        "recipient_agent_id": recipient_agent_id,
                        "bundle_count": len(events),
                        "digest_period_start": period_start.isoformat(),
                        "digest_period_end": period_end.isoformat(),
                    },
                )

    return digests_emitted
