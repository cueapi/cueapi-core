"""Usage enforcement tests — Redis/Postgres fallback, grace period, limit blocking.

11 tests — ported from govindkavaturi-art/cueapi tests/test_qa_usage_enforcement.py
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models.usage_monthly import UsageMonthly
from app.services.usage_service import (
    _current_month_start,
    _month_key,
    check_execution_limit,
    get_monthly_usage,
)
from tests.test_poller import _create_test_user


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

async def _insert_usage(db_session, user_id, count, month_start=None):
    if month_start is None:
        month_start = _current_month_start()
    stmt = (
        pg_insert(UsageMonthly)
        .values(user_id=user_id, month_start=month_start, execution_count=count)
        .on_conflict_do_update(
            constraint="unique_user_month",
            set_={"execution_count": count},
        )
    )
    await db_session.execute(stmt)
    await db_session.commit()


# ===========================================================================
# Tests
# ===========================================================================

@pytest.mark.asyncio
async def test_redis_miss_falls_back_to_postgres(db_session, redis_client):
    """When Redis key absent, usage is read from Postgres."""
    user_id = await _create_test_user(db_session)
    await _insert_usage(db_session, user_id, 250)

    # Don't set Redis key — force cache miss
    usage = await get_monthly_usage(str(user_id), redis_client, db_session)
    assert usage == 250


@pytest.mark.asyncio
async def test_redis_flush_falls_back_to_postgres(db_session, redis_client):
    """After Redis flush, usage still reads correctly from Postgres."""
    user_id = await _create_test_user(db_session)
    month_key = _month_key()
    cache_key = f"usage:{user_id}:{month_key}"

    await redis_client.set(cache_key, "100")
    await _insert_usage(db_session, user_id, 100)

    # Simulate flush
    await redis_client.delete(cache_key)

    usage = await get_monthly_usage(str(user_id), redis_client, db_session)
    assert usage == 100


@pytest.mark.asyncio
async def test_redis_stale_corrected_by_postgres(db_session, redis_client):
    """Cache miss forces Postgres read, which warms Redis with correct value."""
    user_id = await _create_test_user(db_session)
    month_key = _month_key()
    cache_key = f"usage:{user_id}:{month_key}"

    # Postgres has truth, Redis has nothing
    await _insert_usage(db_session, user_id, 200)
    await redis_client.delete(cache_key)

    usage = await get_monthly_usage(str(user_id), redis_client, db_session)
    assert usage == 200

    # Redis should now be warmed
    cached = await redis_client.get(cache_key)
    assert cached is not None
    assert int(cached) == 200


@pytest.mark.asyncio
async def test_under_limit_allowed(db_session, redis_client):
    """Execution allowed when usage < limit."""
    user_id = await _create_test_user(db_session)
    await _insert_usage(db_session, user_id, 100)

    result = await check_execution_limit(str(user_id), 300, redis_client, db_session)
    assert result["allowed"] is True
    assert result["over_limit"] is False
    assert result["usage"] == 100


@pytest.mark.asyncio
async def test_at_limit_grace_starts(db_session, redis_client):
    """First time hitting limit starts grace period and allows delivery."""
    user_id = await _create_test_user(db_session)
    await _insert_usage(db_session, user_id, 300)

    await redis_client.delete(f"grace:{user_id}")

    result = await check_execution_limit(str(user_id), 300, redis_client, db_session)
    assert result["allowed"] is True
    assert result["over_limit"] is True
    assert result["grace_active"] is True

    grace_val = await redis_client.get(f"grace:{user_id}")
    assert grace_val is not None


@pytest.mark.asyncio
async def test_within_grace_allowed(db_session, redis_client):
    """Within 24h grace period, delivery still allowed."""
    user_id = await _create_test_user(db_session)
    await _insert_usage(db_session, user_id, 300)

    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await redis_client.set(f"grace:{user_id}", one_hour_ago, ex=86400)

    result = await check_execution_limit(str(user_id), 300, redis_client, db_session)
    assert result["allowed"] is True
    assert result["grace_active"] is True


@pytest.mark.asyncio
async def test_after_grace_blocked(db_session, redis_client):
    """After 24h grace expires, delivery is blocked."""
    user_id = await _create_test_user(db_session)
    await _insert_usage(db_session, user_id, 300)

    twenty_five_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    await redis_client.set(f"grace:{user_id}", twenty_five_hours_ago, ex=86400)

    result = await check_execution_limit(str(user_id), 300, redis_client, db_session)
    assert result["allowed"] is False
    assert result["over_limit"] is True
    assert result["grace_active"] is False


@pytest.mark.asyncio
async def test_redis_down_postgres_still_works(db_session):
    """Redis down → usage still readable from Postgres."""
    user_id = await _create_test_user(db_session)
    await _insert_usage(db_session, user_id, 200)

    broken_redis = AsyncMock()
    broken_redis.get = AsyncMock(side_effect=ConnectionError("Redis down"))
    broken_redis.set = AsyncMock(side_effect=ConnectionError("Redis down"))

    usage = await get_monthly_usage(str(user_id), broken_redis, db_session)
    assert usage == 200


@pytest.mark.asyncio
async def test_redis_down_grace_check_blocks(db_session):
    """Redis down and can't check grace → default to blocking (safe fail)."""
    user_id = await _create_test_user(db_session)
    await _insert_usage(db_session, user_id, 300)

    broken_redis = AsyncMock()
    broken_redis.get = AsyncMock(side_effect=ConnectionError("Redis down"))
    broken_redis.set = AsyncMock(side_effect=ConnectionError("Redis down"))

    result = await check_execution_limit(str(user_id), 300, broken_redis, db_session)
    assert result["allowed"] is False
    assert result["over_limit"] is True


@pytest.mark.asyncio
async def test_new_month_independent(db_session, redis_client):
    """Usage from a prior month doesn't bleed into current month."""
    user_id = await _create_test_user(db_session)

    # Prior month: 999
    feb_start = date(2026, 2, 1)
    await _insert_usage(db_session, user_id, 999, month_start=feb_start)

    # Current month: 50
    current_start = _current_month_start()
    await _insert_usage(db_session, user_id, 50, month_start=current_start)

    usage = await get_monthly_usage(str(user_id), redis_client, db_session)
    assert usage == 50


@pytest.mark.asyncio
async def test_usage_zero_for_new_user(db_session, redis_client):
    """Brand-new user with no usage records returns 0."""
    user_id = await _create_test_user(db_session)
    usage = await get_monthly_usage(str(user_id), redis_client, db_session)
    assert usage == 0


@pytest.mark.asyncio
async def test_check_limit_zero_limit(db_session, redis_client):
    """Limit of 0 means even 0 usage is over limit (edge case)."""
    user_id = await _create_test_user(db_session)
    # usage=0, limit=0 → over limit
    result = await check_execution_limit(str(user_id), 0, redis_client, db_session)
    assert result["over_limit"] is True
