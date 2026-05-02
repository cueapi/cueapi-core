"""alert_service: create_alert + dedup + consecutive_failures counter."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alert import Alert
from app.models.cue import Cue
from app.models.execution import Execution
from app.models.user import User
from app.services.alert_service import (
    CONSECUTIVE_FAILURE_THRESHOLD,
    count_consecutive_failures,
    create_alert,
)


async def _user(session: AsyncSession):
    suffix = uuid.uuid4().hex[:8]
    u = User(
        email=f"s-{suffix}@test.com",
        api_key_hash=uuid.uuid4().hex,
        api_key_prefix="cue_sk_test",
        webhook_secret="x" * 64,
        slug=f"s-{suffix}",
    )
    session.add(u)
    await session.commit()
    return u


async def _cue(session, user_id):
    c = Cue(
        id=f"cue_{uuid.uuid4().hex[:12]}",
        user_id=user_id,
        name=f"s-{uuid.uuid4().hex[:6]}",
        schedule_type="once",
        schedule_at=datetime.now(timezone.utc) + timedelta(hours=1),
        next_run=datetime.now(timezone.utc) + timedelta(hours=1),
        callback_url="https://example.com/h",
        callback_method="POST",
        callback_transport="webhook",
        status="active",
        payload={},
        retry_max_attempts=3,
        retry_backoff_minutes=[1, 5, 15],
        on_failure={"email": False, "webhook": None, "pause": False},
    )
    session.add(c)
    await session.commit()
    return c


async def _exec(session, cue_id, *, status="pending", minutes_ago=0):
    created = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    ex = Execution(
        id=uuid.uuid4(),
        cue_id=cue_id,
        scheduled_for=created,
        status=status,
    )
    session.add(ex)
    await session.commit()
    # Stamp created_at so ordering in count_consecutive_failures is
    # deterministic when rows are created in a tight loop.
    if minutes_ago:
        await session.execute(
            update(Execution).where(Execution.id == ex.id).values(created_at=created)
        )
        await session.commit()
    return ex


class TestCreateAlert:
    @pytest.mark.asyncio
    async def test_persists(self, db_session):
        u = await _user(db_session)
        a = await create_alert(
            db_session,
            user_id=u.id,
            alert_type="outcome_timeout",
            message="timed out",
            schedule_delivery=False,
        )
        await db_session.commit()
        assert a is not None
        assert a.alert_type == "outcome_timeout"

    @pytest.mark.asyncio
    async def test_dedup_within_window(self, db_session):
        u = await _user(db_session)
        ex_id = uuid.uuid4()
        first = await create_alert(
            db_session,
            user_id=u.id,
            alert_type="verification_failed",
            message="first",
            execution_id=ex_id,
            schedule_delivery=False,
        )
        await db_session.commit()
        second = await create_alert(
            db_session,
            user_id=u.id,
            alert_type="verification_failed",
            message="second",
            execution_id=ex_id,
            schedule_delivery=False,
        )
        await db_session.commit()
        assert first is not None
        assert second is None, "expected dedup to skip the second alert"

        # Only one row persists
        rows = await db_session.execute(
            select(Alert).where(Alert.execution_id == ex_id)
        )
        all_rows = rows.scalars().all()
        assert len(all_rows) == 1

    @pytest.mark.asyncio
    async def test_dedup_does_not_block_different_types(self, db_session):
        u = await _user(db_session)
        ex_id = uuid.uuid4()
        a1 = await create_alert(
            db_session, user_id=u.id, alert_type="verification_failed",
            message="v", execution_id=ex_id, schedule_delivery=False,
        )
        await db_session.commit()
        a2 = await create_alert(
            db_session, user_id=u.id, alert_type="outcome_timeout",
            message="t", execution_id=ex_id, schedule_delivery=False,
        )
        await db_session.commit()
        assert a1 is not None and a2 is not None


class TestConsecutiveFailures:
    @pytest.mark.asyncio
    async def test_streak_counts_contiguous_failures(self, db_session):
        u = await _user(db_session)
        c = await _cue(db_session, u.id)
        # Create in newest-first order so the streak walks backward correctly.
        await _exec(db_session, c.id, status="failed", minutes_ago=1)
        await _exec(db_session, c.id, status="failed", minutes_ago=2)
        await _exec(db_session, c.id, status="failed", minutes_ago=3)
        await _exec(db_session, c.id, status="success", minutes_ago=4)

        streak = await count_consecutive_failures(db_session, c.id)
        assert streak == 3

    @pytest.mark.asyncio
    async def test_streak_breaks_on_success(self, db_session):
        u = await _user(db_session)
        c = await _cue(db_session, u.id)
        await _exec(db_session, c.id, status="failed", minutes_ago=1)
        await _exec(db_session, c.id, status="success", minutes_ago=2)
        await _exec(db_session, c.id, status="failed", minutes_ago=3)

        streak = await count_consecutive_failures(db_session, c.id)
        assert streak == 1

    @pytest.mark.asyncio
    async def test_threshold_constant(self):
        assert CONSECUTIVE_FAILURE_THRESHOLD == 3
