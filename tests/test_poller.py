from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.cue import Cue
from app.models.dispatch_outbox import DispatchOutbox
from app.models.execution import Execution
from app.utils.ids import generate_cue_id


async def _create_test_user(db_session):
    """Create a user directly in DB and return user_id."""
    from app.models.user import User
    from app.utils.ids import generate_api_key, generate_webhook_secret, hash_api_key, get_api_key_prefix

    api_key = generate_api_key()
    suffix = uuid.uuid4().hex[:8]
    user = User(
        email=f"poller-{suffix}@test.com",
        api_key_hash=hash_api_key(api_key),
        api_key_prefix=get_api_key_prefix(api_key),
        webhook_secret=generate_webhook_secret(),
        slug=f"poller-{suffix}",
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user.id


async def _create_due_cue(db_session, user_id, **kwargs):
    """Create a cue with next_run in the past (ready for pickup)."""
    defaults = dict(
        id=generate_cue_id(),
        user_id=user_id,
        name=f"test-cue-{uuid.uuid4().hex[:6]}",
        status="active",
        schedule_type="once",
        schedule_timezone="UTC",
        callback_url="http://localhost:19999/webhook",
        callback_method="POST",
        callback_headers={},
        payload={"test": True},
        retry_max_attempts=3,
        retry_backoff_minutes=[1, 5, 15],
        next_run=datetime.now(timezone.utc) - timedelta(seconds=10),
    )
    defaults.update(kwargs)
    cue = Cue(**defaults)
    db_session.add(cue)
    await db_session.commit()
    await db_session.refresh(cue)
    return cue


async def _fresh_session(db_engine):
    """Get a fresh session for verification queries."""
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    return factory()


@pytest.mark.asyncio
async def test_poller_creates_execution_and_outbox(db_engine, db_session):
    """Poller finds due cues and creates execution + outbox rows atomically."""
    from worker.poller import poll_due_cues

    user_id = await _create_test_user(db_session)
    cue = await _create_due_cue(db_session, user_id)

    count = await poll_due_cues(db_engine, batch_size=500)
    assert count >= 1

    # Use a fresh session to verify
    session = await _fresh_session(db_engine)
    try:
        result = await session.execute(
            select(Execution).where(Execution.cue_id == cue.id)
        )
        execution = result.scalar_one_or_none()
        assert execution is not None
        assert execution.status == "pending"

        outbox_result = await session.execute(
            select(DispatchOutbox).where(DispatchOutbox.cue_id == cue.id)
        )
        outbox = outbox_result.scalar_one_or_none()
        assert outbox is not None
        assert outbox.task_type == "deliver"
        assert outbox.dispatched is False
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_poller_does_not_pick_up_paused_cues(db_engine, db_session):
    from worker.poller import poll_due_cues

    user_id = await _create_test_user(db_session)
    cue = await _create_due_cue(db_session, user_id, status="paused")

    count = await poll_due_cues(db_engine, batch_size=500)

    session = await _fresh_session(db_engine)
    try:
        result = await session.execute(
            select(func.count()).select_from(Execution).where(Execution.cue_id == cue.id)
        )
        assert result.scalar() == 0
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_execution_dedup_prevents_duplicate(db_engine, db_session):
    """Inserting the same (cue_id, scheduled_for) twice should not create two executions."""
    user_id = await _create_test_user(db_session)
    cue = await _create_due_cue(db_session, user_id)
    scheduled = cue.next_run

    session = await _fresh_session(db_engine)
    try:
        # First insert
        result1 = await session.execute(
            pg_insert(Execution)
            .values(cue_id=cue.id, scheduled_for=scheduled, status="pending")
            .on_conflict_do_nothing(index_elements=["cue_id", "scheduled_for"])
            .returning(Execution.id)
        )
        assert result1.fetchone() is not None
        await session.commit()

        # Second insert - should be deduplicated
        result2 = await session.execute(
            pg_insert(Execution)
            .values(cue_id=cue.id, scheduled_for=scheduled, status="pending")
            .on_conflict_do_nothing(index_elements=["cue_id", "scheduled_for"])
            .returning(Execution.id)
        )
        assert result2.fetchone() is None
        await session.commit()
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_poller_updates_recurring_cue_next_run(db_engine, db_session):
    """Recurring cue should have next_run updated after polling."""
    from worker.poller import poll_due_cues

    user_id = await _create_test_user(db_session)
    old_next_run = datetime.now(timezone.utc) - timedelta(seconds=10)
    cue = await _create_due_cue(
        db_session, user_id,
        schedule_type="recurring",
        schedule_cron="* * * * *",
        next_run=old_next_run,
    )

    await poll_due_cues(db_engine, batch_size=500)

    session = await _fresh_session(db_engine)
    try:
        result = await session.execute(select(Cue).where(Cue.id == cue.id))
        updated_cue = result.scalar_one()
        assert updated_cue.next_run is not None
        assert updated_cue.next_run > old_next_run
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_poller_sets_onetime_cue_next_run_null(db_engine, db_session):
    """One-time cue should have next_run set to NULL after polling."""
    from worker.poller import poll_due_cues

    user_id = await _create_test_user(db_session)
    cue = await _create_due_cue(db_session, user_id, schedule_type="once")

    await poll_due_cues(db_engine, batch_size=500)

    session = await _fresh_session(db_engine)
    try:
        result = await session.execute(select(Cue).where(Cue.id == cue.id))
        updated_cue = result.scalar_one()
        assert updated_cue.next_run is None
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_drain_loop_processes_multiple_batches(db_engine, db_session):
    """If there are more due cues than batch_size, drain loop handles them all."""
    from worker.poller import poll_due_cues

    user_id = await _create_test_user(db_session)
    for _ in range(10):
        await _create_due_cue(db_session, user_id)

    # Use tiny batch size to force multiple iterations
    total = await poll_due_cues(db_engine, batch_size=3)
    assert total == 10
