"""Message quota + rate-limit enforcement.

Spec: `https://trydock.ai/mike/cueapi-messaging-primitive-v1-sp` §7 (Quotas + abuse) +
§13 D1 (separate monthly_message_limit) + D5 (free=300/pro=5000/scale=50000).

Mirrors the shape of ``app/services/usage_service.py`` (cue execution
counts) — dual-write to Redis (fast) + Postgres (durable). Postgres is
the source of truth; Redis is the cache. Cache-miss fallback to
Postgres + cache warm; Redis-down fallback to Postgres-direct.

Three rate-limit dimensions enforced at message create time (§7.3):

* **Per-minute rate limit** — sliding window. Plan-tiered (free=10/min,
  pro=60/min, scale=300/min). Redis sorted set ``msg_ratelimit:{user_id}``.
* **Monthly quota** — ``monthly_message_limit`` from User row.
  Postgres-durable counter; Redis cache ``msg_quota:{user_id}:{YYYY-MM-01}``.
  Atomic increment via PostgreSQL UPSERT ``ON CONFLICT ... DO UPDATE``
  with ``RETURNING execution_count`` (write-through).
* **Priority-high anti-abuse** — priority>3 capped at 10/hour/sender
  globally + 5/hour for any single sender→recipient pair. Excess
  inbound priority>3 is downgraded to priority=3 on the message row
  AND the response surfaces ``X-CueAPI-Priority-Downgraded: true``.

Failure mode for Redis: every operation wraps in try/except. Redis
unavailable means we lose the per-minute window (allow through), but
we still have the monthly quota check via Postgres. Same Redis-down
philosophy as ``rate_limit.py``.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Tuple

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import UsageMessagesMonthly, User

logger = logging.getLogger(__name__)


def _current_month_start() -> date:
    now = datetime.now(timezone.utc)
    return date(now.year, now.month, 1)


def _month_key() -> str:
    return _current_month_start().isoformat()


# Plan-tier per-minute rate limits (messages/min). Mirrors the shape
# of cue rate_limit_per_minute on User but keyed independently per
# §13 D1 (separate quotas).
PER_MINUTE_BY_PLAN = {
    "free": 10,
    "pro": 60,
    "scale": 300,
}
PRIORITY_HIGH_PER_HOUR_PER_SENDER = 10
PRIORITY_HIGH_PER_HOUR_PER_PAIR = 5


def _http_error(status: int, code: str, message: str, **extra) -> HTTPException:
    detail = {"error": {"code": code, "message": message, "status": status}}
    detail["error"].update(extra)
    return HTTPException(status_code=status, detail=detail)


# ---- Monthly quota ------------------------------------------------------


async def get_monthly_message_count(
    user_id, redis, db_session: Optional[AsyncSession] = None
) -> int:
    """Read monthly count. Redis first; Postgres fallback + warm."""
    month_key = _month_key()
    cache_key = f"msg_quota:{user_id}:{month_key}"
    try:
        cached = await redis.get(cache_key)
        if cached is not None:
            return int(cached)
    except Exception:
        pass

    if db_session is None:
        return 0

    try:
        result = await db_session.execute(
            select(UsageMessagesMonthly.message_count).where(
                UsageMessagesMonthly.user_id == user_id,
                UsageMessagesMonthly.month_start == _current_month_start(),
            )
        )
        row = result.scalar_one_or_none()
        count = int(row or 0)
    except Exception:
        return 0

    # Warm cache (best-effort).
    try:
        await redis.setex(cache_key, 35 * 24 * 3600, count)
    except Exception:
        pass
    return count


async def check_message_quota(
    db: AsyncSession,
    user_id,
    monthly_limit: int,
    redis,
) -> None:
    """Raise 402 ``quota_exceeded`` if user is at or above their monthly
    message limit. Stricter than execution quota — no 24h grace window
    for messages (§7.3 step 2)."""
    count = await get_monthly_message_count(user_id, redis, db_session=db)
    if count >= monthly_limit:
        raise _http_error(
            402,
            "quota_exceeded",
            f"Monthly message quota of {monthly_limit} exceeded ({count} sent this month).",
            current=count,
            limit=monthly_limit,
        )


async def increment_monthly_count(
    db: AsyncSession,
    user_id,
    redis,
) -> int:
    """Atomic increment via Postgres UPSERT + Redis write-through.

    Returns the post-increment count. Postgres is source of truth;
    Redis cache mirrors the authoritative value (write-through avoids
    the stale-cache race condition that bit cue execution counts in
    QA 1.2).
    """
    month_start = _current_month_start()

    insert_stmt = pg_insert(UsageMessagesMonthly).values(
        user_id=user_id,
        month_start=month_start,
        message_count=1,
    )
    upsert = insert_stmt.on_conflict_do_update(
        index_elements=["user_id", "month_start"],
        set_={"message_count": UsageMessagesMonthly.message_count + 1},
    ).returning(UsageMessagesMonthly.message_count)
    result = await db.execute(upsert)
    new_count = int(result.scalar_one())
    # Commit the UPSERT so the increment is visible to subsequent
    # quota checks. Caller already committed the message insert
    # earlier; this is a separate transaction.
    await db.commit()

    # Redis write-through (best-effort).
    cache_key = f"msg_quota:{user_id}:{_month_key()}"
    try:
        await redis.setex(cache_key, 35 * 24 * 3600, new_count)
    except Exception:
        pass

    return new_count


# ---- Per-minute rate limit (sliding window) ----------------------------


async def check_per_minute_rate_limit(
    user_id,
    plan: str,
    redis,
) -> None:
    """Sliding window check. Adds a unique entry on success.

    Same shape as the existing global RateLimitMiddleware but keyed on
    ``msg_ratelimit:{user_id}`` so message rate limits are independent
    from the global per-key rate limit. Both apply to POST /v1/messages
    — caller hits whichever is lower first.
    """
    limit = PER_MINUTE_BY_PLAN.get(plan, PER_MINUTE_BY_PLAN["free"])
    key = f"msg_ratelimit:{user_id}"
    now = time.time()
    window_start = now - 60

    try:
        # Drop entries older than 60s.
        await redis.zremrangebyscore(key, 0, window_start)
        count = await redis.zcard(key)
        if count >= limit:
            raise _http_error(
                429,
                "rate_limit_exceeded",
                f"Message rate limit ({limit}/min) exceeded.",
                limit=limit,
                window_seconds=60,
            )
        # Add unique entry — id-suffix makes burst-time-collisions unique.
        import uuid
        member = f"{now}:{uuid.uuid4().hex[:8]}"
        await redis.zadd(key, {member: now})
        await redis.expire(key, 120)
    except HTTPException:
        raise
    except Exception:
        # Redis down — fail open. Postgres-side quota still applies.
        logger.warning("Redis unavailable for message rate limit; allowing through", exc_info=False)


# ---- Priority high anti-abuse ------------------------------------------


async def check_priority_high_limits(
    *,
    user_id,
    from_agent_id: str,
    to_agent_id: str,
    priority: int,
    redis,
) -> Tuple[int, bool]:
    """Returns (effective_priority, was_downgraded).

    For priority<=3, no-op. For priority>3:
    - Sender-side: 10/hour. On exceed → 429.
    - Pair-level: 5/hour from this sender to this recipient. On exceed
      → DOWNGRADE to priority=3 silently (with response header signal),
      not a hard reject.

    Implementation uses simple counter keys with TTL rather than
    sorted sets — coarser semantics (per-hour, not sliding) but cheap
    and sufficient for anti-abuse.
    """
    if priority <= 3:
        return priority, False

    sender_key = f"msg_priority_high:{user_id}"
    pair_key = f"msg_inbound_priority:{from_agent_id}:{to_agent_id}"

    try:
        # Sender-side hard limit.
        sender_count = await redis.incr(sender_key)
        if sender_count == 1:
            await redis.expire(sender_key, 3600)
        if sender_count > PRIORITY_HIGH_PER_HOUR_PER_SENDER:
            raise _http_error(
                429,
                "priority_high_rate_limit",
                f"High-priority send limit ({PRIORITY_HIGH_PER_HOUR_PER_SENDER}/hour) exceeded.",
                limit=PRIORITY_HIGH_PER_HOUR_PER_SENDER,
                window_seconds=3600,
            )

        # Pair-level: downgrade rather than reject.
        pair_count = await redis.incr(pair_key)
        if pair_count == 1:
            await redis.expire(pair_key, 3600)
        if pair_count > PRIORITY_HIGH_PER_HOUR_PER_PAIR:
            return 3, True

    except HTTPException:
        raise
    except Exception:
        # Redis down — allow through at requested priority.
        logger.warning("Redis unavailable for priority anti-abuse; passing priority unchanged", exc_info=False)
        return priority, False

    return priority, False


# ---- Plan-tier resolution ----------------------------------------------


async def get_user_plan_and_msg_limit(
    db: AsyncSession, user_id
) -> Tuple[str, int]:
    """Return (plan, monthly_message_limit) for the user."""
    result = await db.execute(
        select(User.plan, User.monthly_message_limit).where(User.id == user_id)
    )
    row = result.fetchone()
    if not row:
        # Should be impossible — caller is authenticated.
        return "free", 300
    return row.plan or "free", int(row.monthly_message_limit or 300)
