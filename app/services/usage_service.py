from __future__ import annotations

import calendar
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cue import Cue
from app.models.execution import Execution
from app.models.usage_monthly import UsageMonthly

logger = logging.getLogger(__name__)


def _current_month_start() -> date:
    """Return first day of current UTC month as a date."""
    now = datetime.now(timezone.utc)
    return date(now.year, now.month, 1)


def _month_key() -> str:
    """Return current month key string like '2026-03-01'."""
    return _current_month_start().isoformat()


async def get_monthly_usage(user_id: str, redis, db_session: Optional[AsyncSession] = None) -> int:
    """Get current month's execution count. Redis first, Postgres fallback, warm cache.

    If Redis has the value, returns it immediately. On Redis miss or Redis down,
    falls back to Postgres (source of truth) and warms the cache. If neither is
    available, returns 0.
    """
    month_key = _month_key()
    cache_key = f"usage:{user_id}:{month_key}"

    # Try Redis first
    try:
        cached = await redis.get(cache_key)
        if cached is not None:
            return int(cached)
    except Exception:
        pass  # Redis down, fall through to Postgres

    # Cache miss or Redis down — query Postgres (source of truth)
    if db_session is not None:
        result = await db_session.execute(
            select(UsageMonthly.execution_count).where(
                UsageMonthly.user_id == user_id,
                UsageMonthly.month_start == _current_month_start(),
            )
        )
        count = result.scalar_one_or_none() or 0

        # Warm cache (best effort, don't fail if Redis is down)
        try:
            await redis.set(cache_key, str(count), ex=86400)
        except Exception:
            pass

        return count

    return 0


async def warm_usage_cache(user_id: str, redis, db_session: AsyncSession) -> int:
    """Query DB for usage count and warm the Redis cache."""
    month_start = _current_month_start()
    result = await db_session.execute(
        select(UsageMonthly.execution_count).where(
            UsageMonthly.user_id == user_id,
            UsageMonthly.month_start == month_start,
        )
    )
    row = result.fetchone()
    count = row.execution_count if row else 0

    try:
        cache_key = f"usage:{user_id}:{_month_key()}"
        await redis.set(cache_key, str(count), ex=86400)  # 24h TTL
    except Exception:
        pass

    return count


async def increment_usage(user_id: str, redis, db_session: AsyncSession):
    """Increment usage in both Redis (fast) and Postgres (durable).

    Conflict handling:
    - Both succeed: normal path
    - Redis succeeds, Postgres fails: Redis is ahead. Next Postgres read
      will be lower. get_monthly_usage will warm from Postgres (lower value).
      Temporary under-enforcement until next successful Postgres write.
    - Redis fails, Postgres succeeds: Redis is behind. Next cache miss will
      read from Postgres (correct value) and warm Redis.
    - Both fail: usage not tracked for this execution. Under-enforcement by 1.

    In all failure cases, error is temporary and self-correcting on next cycle.
    """
    month_start = _current_month_start()
    cache_key = f"usage:{user_id}:{_month_key()}"

    # Redis increment (fast, best effort)
    try:
        pipe = redis.pipeline()
        pipe.incr(cache_key)
        pipe.expire(cache_key, 35 * 86400)
        await pipe.execute()
    except Exception as e:
        logger.warning(f"Redis usage increment failed for {user_id}: {e}")

    # Postgres increment (durable, must succeed)
    try:
        stmt = (
            pg_insert(UsageMonthly)
            .values(user_id=user_id, month_start=month_start, execution_count=1)
            .on_conflict_do_update(
                constraint="unique_user_month",
                set_={"execution_count": UsageMonthly.execution_count + 1},
            )
            .returning(UsageMonthly.execution_count)
        )
        result = await db_session.execute(stmt)
        row = result.fetchone()
        await db_session.commit()

        # Write-through: sync Redis with authoritative Postgres value immediately
        # This ensures the next limit check sees the updated count within the same second
        if row:
            try:
                await redis.set(cache_key, str(row.execution_count), ex=35 * 86400)
            except Exception:
                pass  # Redis write failed, next read will fall through to Postgres
    except Exception as e:
        logger.error(f"Postgres usage increment failed for {user_id}: {e}")


async def check_execution_limit(
    user_id: str, monthly_limit: int, redis, db_session: Optional[AsyncSession] = None
) -> dict:
    """Check if user is within execution limits.

    Returns dict with:
        allowed: bool - whether execution should proceed
        over_limit: bool - whether user is over limit
        grace_active: bool - whether grace period is active
        usage: int - current usage count

    Grace period behavior:
    - First time hitting limit → start 24h grace, allow delivery
    - Within 24h grace → allow delivery
    - After 24h grace → hard block
    - Redis down and can't check grace → default to blocking (safe)
    """
    usage = await get_monthly_usage(user_id, redis, db_session)

    if usage < monthly_limit:
        return {"allowed": True, "over_limit": False, "grace_active": False, "usage": usage}

    # Over limit — check grace period
    grace_key = f"grace:{user_id}"

    try:
        grace_start = await redis.get(grace_key)
    except Exception:
        # Redis down — can't check grace, default to blocking
        return {"allowed": False, "over_limit": True, "grace_active": False, "usage": usage}

    if grace_start is None:
        # First time hitting limit — start grace period (24 hours)
        try:
            await redis.set(grace_key, datetime.now(timezone.utc).isoformat(), ex=86400)
        except Exception:
            pass
        return {"allowed": True, "over_limit": True, "grace_active": True, "usage": usage}

    # Grace period exists — check if expired
    grace_dt = datetime.fromisoformat(grace_start)
    elapsed = (datetime.now(timezone.utc) - grace_dt).total_seconds()

    if elapsed <= 86400:
        # Still within grace period
        return {"allowed": True, "over_limit": True, "grace_active": True, "usage": usage}
    else:
        # Grace expired — hard block
        return {"allowed": False, "over_limit": True, "grace_active": False, "usage": usage}


async def get_outcome_summary(db_session: AsyncSession, user_id: str) -> dict:
    """Get outcome summary for current month's executions."""
    month_start = _current_month_start()

    # Count executions this month that belong to this user's cues
    base_query = (
        select(func.count())
        .select_from(Execution)
        .join(Cue, Execution.cue_id == Cue.id)
        .where(
            Cue.user_id == user_id,
            Execution.created_at >= datetime(month_start.year, month_start.month, month_start.day, tzinfo=timezone.utc),
        )
    )

    # Total with outcomes reported
    reported_result = await db_session.execute(
        base_query.where(Execution.outcome_recorded_at.isnot(None))
    )
    reported = reported_result.scalar() or 0

    # Succeeded outcomes
    success_result = await db_session.execute(
        base_query.where(
            Execution.outcome_recorded_at.isnot(None),
            Execution.outcome_success.is_(True),
        )
    )
    succeeded = success_result.scalar() or 0

    # Failed outcomes — includes both reported failures (outcome_success=False)
    # and delivery failures (status='failed' or 'missed') with no outcome reported
    failed_result = await db_session.execute(
        base_query.where(
            Execution.outcome_recorded_at.isnot(None),
            Execution.outcome_success.is_(False),
        )
    )
    failed_reported = failed_result.scalar() or 0

    failed_delivery_result = await db_session.execute(
        base_query.where(
            Execution.outcome_recorded_at.is_(None),
            Execution.status.in_(["failed", "missed"]),
        )
    )
    failed_delivery = failed_delivery_result.scalar() or 0

    failed = failed_reported + failed_delivery

    return {
        "reported": reported,
        "succeeded": succeeded,
        "failed": failed,
    }


def _days_remaining(period_end: date) -> int:
    """Days remaining in billing period."""
    today = datetime.now(timezone.utc).date()
    return max(0, (period_end - today).days)


def _days_elapsed(period_start: date) -> int:
    """Days elapsed in billing period (min 1 to avoid div-by-zero)."""
    today = datetime.now(timezone.utc).date()
    return max(1, (today - period_start).days + 1)


def _projected_month_end(used: int, days_elapsed: int, total_days: int) -> int:
    """Linear projection of month-end usage."""
    if days_elapsed <= 0:
        return used
    return round((used / days_elapsed) * total_days)


def _percent_used(used: int, limit: int) -> int:
    """Percentage of limit used, as integer."""
    if limit <= 0:
        return 0
    return round((used / limit) * 100)


async def _get_current_rate_usage(ratelimit_key: Optional[str], redis) -> int:
    """Get current request count in the rate limit sliding window."""
    if not ratelimit_key:
        return 0
    try:
        import time
        now = time.time()
        window_start = now - 60
        pipe = redis.pipeline()
        pipe.zremrangebyscore(ratelimit_key, 0, window_start)
        pipe.zcard(ratelimit_key)
        results = await pipe.execute()
        return results[1]
    except Exception:
        return 0


async def get_usage_stats(
    user_id: str, redis, db_session: AsyncSession, user,
    ratelimit_key: Optional[str] = None,
) -> dict:
    """Build usage stats response for GET /v1/usage."""
    month_start = _current_month_start()

    # Get execution usage with Postgres fallback
    execution_count = await get_monthly_usage(user_id, redis, db_session)

    # Get active cue count
    cue_result = await db_session.execute(
        select(func.count())
        .select_from(Cue)
        .where(Cue.user_id == user_id, Cue.status.in_(["active", "paused"]))
    )
    active_cues = cue_result.scalar()

    # Calculate period end (last day of month)
    if month_start.month == 12:
        period_end = date(month_start.year + 1, 1, 1)
    else:
        period_end = date(month_start.year, month_start.month + 1, 1)
    period_end = period_end - timedelta(days=1)

    limit = user.monthly_execution_limit
    pct = round((execution_count / limit) * 100, 1) if limit > 0 else 0
    total_days = calendar.monthrange(month_start.year, month_start.month)[1]
    days_elapsed = _days_elapsed(month_start)
    projected = _projected_month_end(execution_count, days_elapsed, total_days)

    # Get outcome summary for current month
    outcome_summary = await get_outcome_summary(db_session, user_id)

    # Get current rate limit usage
    current_rate = await _get_current_rate_usage(ratelimit_key, redis)

    cue_limit = user.active_cue_limit

    return {
        "plan": user.plan,
        "billing_period": {
            "start": month_start.isoformat(),
            "end": period_end.isoformat(),
            "days_remaining": _days_remaining(period_end),
        },
        # Deprecated alias — remove after dashboard migrates
        "period": {
            "start": month_start.isoformat(),
            "end": period_end.isoformat(),
        },
        "cues": {
            "used": active_cues,
            "active": active_cues,  # deprecated alias
            "limit": cue_limit,
            "remaining": max(0, cue_limit - active_cues),
            "percent_used": _percent_used(active_cues, cue_limit),
        },
        "executions": {
            "used": execution_count,
            "limit": limit,
            "remaining": max(0, limit - execution_count),
            "percent_used": _percent_used(execution_count, limit),
            "percentage": pct,  # deprecated alias
            "projected_month_end": projected,
            "will_exceed_limit": projected >= limit,
        },
        "outcomes": outcome_summary,
        "rate_limit": {
            "requests_per_minute": user.rate_limit_per_minute,
            "current_usage": current_rate,
        },
        "upgrade_url": "https://dashboard.cueapi.ai/billing",
    }
