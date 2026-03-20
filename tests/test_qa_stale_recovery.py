from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.cue import Cue
from app.models.execution import Execution
from tests.test_poller import _create_due_cue, _create_test_user, _fresh_session
from worker.poller import recover_stale_executions


async def _create_stale_execution(db_session, cue_id, attempts=0, stale_seconds=600):
    """Create an execution stuck in 'delivering' with old updated_at."""
    execution = Execution(
        id=uuid.uuid4(),
        cue_id=cue_id,
        scheduled_for=datetime.now(timezone.utc) - timedelta(seconds=stale_seconds + 100),
        status="delivering",
        attempts=attempts,
        started_at=datetime.now(timezone.utc) - timedelta(seconds=stale_seconds + 50),
    )
    db_session.add(execution)
    await db_session.commit()
    await db_session.refresh(execution)

    # Backdate updated_at so it looks stale
    async with db_session.bind.begin() if hasattr(db_session, 'bind') else _noop():
        pass
    await db_session.execute(
        update(Execution)
        .where(Execution.id == execution.id)
        .values(updated_at=datetime.now(timezone.utc) - timedelta(seconds=stale_seconds + 10))
    )
    await db_session.commit()
    await db_session.refresh(execution)
    return execution


class _noop:
    async def __aenter__(self): pass
    async def __aexit__(self, *a): pass


@pytest.mark.asyncio
async def test_stale_delivering_recovered_to_retrying(db_engine, db_session):
    """Execution stuck in 'delivering' with retries remaining → status='retrying'."""
    user_id = await _create_test_user(db_session)
    cue = await _create_due_cue(
        db_session, user_id, retry_max_attempts=3, retry_backoff_minutes=[1, 5, 15],
    )
    execution = await _create_stale_execution(db_session, cue.id, attempts=1, stale_seconds=600)

    count = await recover_stale_executions(db_engine, stale_seconds=300)
    assert count >= 1

    session = await _fresh_session(db_engine)
    try:
        result = await session.execute(
            select(Execution).where(Execution.id == execution.id)
        )
        updated = result.scalar_one()
        assert updated.status == "retrying"
        assert updated.next_retry is not None
        assert "Recovered from stale" in updated.error_message
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_stale_delivering_failed_when_max_attempts(db_engine, db_session):
    """Execution stuck in 'delivering' with max attempts exhausted → status='failed'."""
    user_id = await _create_test_user(db_session)
    cue = await _create_due_cue(
        db_session, user_id, retry_max_attempts=2, retry_backoff_minutes=[1, 5],
    )
    execution = await _create_stale_execution(db_session, cue.id, attempts=2, stale_seconds=600)

    count = await recover_stale_executions(db_engine, stale_seconds=300)
    assert count >= 1

    session = await _fresh_session(db_engine)
    try:
        result = await session.execute(
            select(Execution).where(Execution.id == execution.id)
        )
        updated = result.scalar_one()
        assert updated.status == "failed"
        assert updated.next_retry is None
        assert "max attempts exhausted" in updated.error_message
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_fresh_delivering_not_recovered(db_engine, db_session):
    """Execution in 'delivering' with recent updated_at should NOT be recovered."""
    user_id = await _create_test_user(db_session)
    cue = await _create_due_cue(db_session, user_id, retry_max_attempts=3)

    # Create execution in delivering with recent updated_at (not stale)
    execution = Execution(
        id=uuid.uuid4(),
        cue_id=cue.id,
        scheduled_for=datetime.now(timezone.utc) - timedelta(seconds=10),
        status="delivering",
        attempts=0,
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(execution)
    await db_session.commit()
    await db_session.refresh(execution)

    count = await recover_stale_executions(db_engine, stale_seconds=300)
    assert count == 0

    session = await _fresh_session(db_engine)
    try:
        result = await session.execute(
            select(Execution).where(Execution.id == execution.id)
        )
        updated = result.scalar_one()
        assert updated.status == "delivering"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_stale_recovery_fails_onetime_cue(db_engine, db_session):
    """Stale one-time cue execution with max attempts → cue also marked 'failed'."""
    user_id = await _create_test_user(db_session)
    cue = await _create_due_cue(
        db_session, user_id,
        schedule_type="once",
        retry_max_attempts=1,
        retry_backoff_minutes=[1],
    )
    execution = await _create_stale_execution(db_session, cue.id, attempts=1, stale_seconds=600)

    await recover_stale_executions(db_engine, stale_seconds=300)

    session = await _fresh_session(db_engine)
    try:
        result = await session.execute(
            select(Execution).where(Execution.id == execution.id)
        )
        updated_exec = result.scalar_one()
        assert updated_exec.status == "failed"

        cue_result = await session.execute(
            select(Cue).where(Cue.id == cue.id)
        )
        updated_cue = cue_result.scalar_one()
        assert updated_cue.status == "failed"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_recover_returns_count(db_engine, db_session):
    """recover_stale_executions returns the number of recovered executions."""
    user_id = await _create_test_user(db_session)
    cue = await _create_due_cue(
        db_session, user_id, retry_max_attempts=5, retry_backoff_minutes=[1, 5, 15],
    )

    for i in range(3):
        await _create_stale_execution(db_session, cue.id, attempts=i, stale_seconds=600)

    count = await recover_stale_executions(db_engine, stale_seconds=300)
    assert count == 3
