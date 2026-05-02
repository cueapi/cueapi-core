"""Messaging-primitive cleanup tasks (OSS port).

Three periodic tasks that keep the messages table tidy:

* ``expire_old_messages`` — TTL transition. Sets ``delivery_state =
  'expired'`` on messages whose ``expires_at < now()`` and which
  aren't already in a terminal state. Row stays in the table so
  post-mortem queries (sender's sent view) still find it.
* ``cleanup_expired_messages`` — hard-delete 7 days after a message
  reaches a terminal state (``acked`` or ``expired``). Batched for
  large tables.
* ``free_old_idempotency_keys`` — NULL out ``idempotency_key`` on
  messages older than 24h so the unique partial index frees the key
  for reuse. PostgreSQL can't put ``NOW()`` in a partial-index
  predicate (IMMUTABLE), so the dedup window is enforced at the
  application layer + this cleanup task makes the keys actually
  reusable after the window closes.

OSS port note: in the private monorepo these functions live in
``worker/gdpr_cleanup.py`` alongside hosted-only GDPR-cascade tasks
and have a dry-run-by-default safety harness. cueapi-core's port
splits them into this messaging-specific module without the
hosted-only safety helpers — self-hosters call with explicit
``dry_run=False`` when they want real action.

Wire these into a scheduler of your choice (cron, arq cron job,
systemd timer, etc.). Recommended cadence:
* expire_old_messages: hourly
* cleanup_expired_messages: daily
* free_old_idempotency_keys: hourly
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message import Message

logger = logging.getLogger(__name__)


# Tuning constants. Self-hosters can override by editing this file or
# by wrapping these functions with their own knobs.
BATCH_SIZE = 1000
MESSAGE_HARD_DELETE_DAYS = 7
IDEMPOTENCY_KEY_WINDOW_HOURS = 24

MESSAGE_TERMINAL_STATES = frozenset({"acked", "expired"})


async def expire_old_messages(
    session: AsyncSession,
    dry_run: bool = True,
) -> dict:
    """Transition ``delivery_state`` to ``'expired'`` for messages where
    ``expires_at < now()`` AND state is not already terminal.

    State-transition cleanup, not a deletion — the row stays in the
    table so post-mortem queries (sender's sent view, audit) still
    work. Hard-deletion happens later via ``cleanup_expired_messages``.

    Args:
        session: AsyncSession (transaction-scoped).
        dry_run: If True (default), counts eligible messages without
            changing them. Pass False to actually perform the
            transition. No env-var safety in OSS — self-hosters opt in
            explicitly.

    Returns dict with:
        dry_run: bool
        eligible_count: int
        transitioned_count: int (0 in dry_run mode)
    """
    now = datetime.now(timezone.utc)

    count_q = (
        select(func.count())
        .select_from(Message)
        .where(
            Message.expires_at < now,
            Message.delivery_state.notin_(list(MESSAGE_TERMINAL_STATES)),
        )
    )
    eligible = (await session.execute(count_q)).scalar() or 0

    result: dict = {
        "dry_run": dry_run,
        "eligible_count": eligible,
        "transitioned_count": 0,
    }

    if dry_run:
        logger.info(
            "Message expiry (DRY RUN): %d messages past expires_at and not terminal.",
            eligible,
        )
        return result

    upd = (
        update(Message)
        .where(
            Message.expires_at < now,
            Message.delivery_state.notin_(list(MESSAGE_TERMINAL_STATES)),
        )
        .values(delivery_state="expired")
    )
    upd_result = await session.execute(upd)
    await session.commit()
    result["transitioned_count"] = upd_result.rowcount or 0
    logger.info(
        "Message expiry: transitioned %d messages to 'expired'.",
        result["transitioned_count"],
    )
    return result


async def cleanup_expired_messages(
    session: AsyncSession,
    dry_run: bool = True,
) -> dict:
    """Hard-delete messages 7 days after they reached a terminal state
    (acked or expired).

    Eligibility:
        - delivery_state == 'acked' AND acked_at < now() - 7d
        - OR delivery_state == 'expired' AND expires_at < now() - 7d

    Args:
        session: AsyncSession.
        dry_run: If True (default), only counts. False = actually
            delete in batches of 1000.

    Returns dict with dry_run, eligible_count, deleted_count, sample_ids.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=MESSAGE_HARD_DELETE_DAYS)

    where_clause = or_(
        and_(Message.delivery_state == "acked", Message.acked_at < cutoff),
        and_(Message.delivery_state == "expired", Message.expires_at < cutoff),
    )

    count_q = select(func.count()).select_from(Message).where(where_clause)
    eligible = (await session.execute(count_q)).scalar() or 0

    sample_q = select(Message.id).where(where_clause).limit(5)
    samples = list((await session.execute(sample_q)).scalars().all())

    result: dict = {
        "dry_run": dry_run,
        "eligible_count": eligible,
        "deleted_count": 0,
        "sample_ids": samples,
    }

    if dry_run:
        logger.info(
            "Message hard-delete (DRY RUN): %d eligible. Sample: %s",
            eligible, samples,
        )
        return result

    total = 0
    while True:
        batch_q = select(Message.id).where(where_clause).limit(BATCH_SIZE)
        batch_ids = (await session.execute(batch_q)).scalars().all()
        if not batch_ids:
            break
        del_stmt = delete(Message).where(Message.id.in_(batch_ids))
        del_result = await session.execute(del_stmt)
        await session.commit()
        total += del_result.rowcount or 0
        logger.info(
            "Message hard-delete: batch deleted %d (running total %d).",
            del_result.rowcount or 0, total,
        )

    result["deleted_count"] = total
    return result


async def free_old_idempotency_keys(
    session: AsyncSession,
    dry_run: bool = True,
) -> dict:
    """NULL out ``idempotency_key`` on messages older than the dedup
    window (24h) so the unique partial index frees the key for reuse.

    The dedup window is enforced at the application layer (in
    ``message_service.create_message`` — only matches existing rows
    where ``created_at > now() - 24h``). This cleanup task makes the
    application-level window actually free the keys for reuse.

    Args:
        session: AsyncSession.
        dry_run: If True (default), only counts. False = NULL the keys.

    Returns dict with dry_run, eligible_count, freed_count.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=IDEMPOTENCY_KEY_WINDOW_HOURS)

    count_q = (
        select(func.count())
        .select_from(Message)
        .where(
            Message.idempotency_key.is_not(None),
            Message.created_at < cutoff,
        )
    )
    eligible = (await session.execute(count_q)).scalar() or 0

    result: dict = {
        "dry_run": dry_run,
        "eligible_count": eligible,
        "freed_count": 0,
    }

    if dry_run:
        logger.info(
            "Idempotency-Key freeing (DRY RUN): %d keys older than %dh.",
            eligible, IDEMPOTENCY_KEY_WINDOW_HOURS,
        )
        return result

    upd = (
        update(Message)
        .where(
            Message.idempotency_key.is_not(None),
            Message.created_at < cutoff,
        )
        .values(idempotency_key=None)
    )
    upd_result = await session.execute(upd)
    await session.commit()
    result["freed_count"] = upd_result.rowcount or 0
    logger.info(
        "Idempotency-Key freeing: cleared %d keys.",
        result["freed_count"],
    )
    return result
