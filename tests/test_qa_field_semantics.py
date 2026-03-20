from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.cue import Cue
from app.models.execution import Execution
from tests.test_poller import _create_due_cue, _create_test_user, _fresh_session
from tests.test_webhook_delivery import _run_full_cycle, start_webhook_receiver
from worker.tasks import _claim_execution


@pytest.mark.asyncio
async def test_started_at_set_on_first_claim(db_engine, db_session):
    """First claim (from pending) sets started_at."""
    user_id = await _create_test_user(db_session)
    cue = await _create_due_cue(db_session, user_id)

    execution = Execution(
        id=uuid.uuid4(),
        cue_id=cue.id,
        scheduled_for=datetime.now(timezone.utc) - timedelta(seconds=10),
        status="pending",
        attempts=0,
    )
    db_session.add(execution)
    await db_session.commit()
    await db_session.refresh(execution)
    assert execution.started_at is None

    session = await _fresh_session(db_engine)
    try:
        claimed = await _claim_execution(session, str(execution.id), "pending")
        assert claimed is True

        result = await session.execute(
            select(Execution).where(Execution.id == execution.id)
        )
        updated = result.scalar_one()
        assert updated.started_at is not None
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_started_at_preserved_on_retry_claim(db_engine, db_session):
    """Retry claim preserves the original started_at timestamp."""
    user_id = await _create_test_user(db_session)
    cue = await _create_due_cue(db_session, user_id)

    original_started = datetime.now(timezone.utc) - timedelta(minutes=5)
    execution = Execution(
        id=uuid.uuid4(),
        cue_id=cue.id,
        scheduled_for=datetime.now(timezone.utc) - timedelta(seconds=10),
        status="retry_ready",
        attempts=1,
        started_at=original_started,
    )
    db_session.add(execution)
    await db_session.commit()
    await db_session.refresh(execution)

    session = await _fresh_session(db_engine)
    try:
        claimed = await _claim_execution(session, str(execution.id), "retry_ready")
        assert claimed is True

        result = await session.execute(
            select(Execution).where(Execution.id == execution.id)
        )
        updated = result.scalar_one()
        # started_at should be preserved (not overwritten with now())
        assert updated.started_at is not None
        # Allow 1 second tolerance for DB timestamp precision
        diff = abs((updated.started_at.replace(tzinfo=timezone.utc) - original_started).total_seconds())
        assert diff < 2, f"started_at changed by {diff}s, should be preserved"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_next_retry_cleared_on_success(db_engine, db_session):
    """After successful delivery, next_retry should be None."""
    received, runner = await start_webhook_receiver(9990)
    try:
        user_id = await _create_test_user(db_session)
        cue = await _create_due_cue(
            db_session, user_id,
            callback_url="http://localhost:9990/webhook",
        )

        await _run_full_cycle(db_engine, cue_id=cue.id)

        session = await _fresh_session(db_engine)
        try:
            result = await session.execute(
                select(Execution).where(Execution.cue_id == cue.id)
            )
            execution = result.scalar_one()
            assert execution.status == "success"
            assert execution.next_retry is None
        finally:
            await session.close()
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_delivered_at_only_on_success(db_engine, db_session):
    """delivered_at is set on success but remains None on failure."""
    # Test failure case — no server listening
    user_id = await _create_test_user(db_session)
    cue = await _create_due_cue(
        db_session, user_id,
        callback_url="http://localhost:19999/nothing",
        retry_max_attempts=1,
        retry_backoff_minutes=[0],
    )

    await _run_full_cycle(db_engine, cue_id=cue.id)

    session = await _fresh_session(db_engine)
    try:
        result = await session.execute(
            select(Execution).where(Execution.cue_id == cue.id)
        )
        execution = result.scalar_one()
        assert execution.status == "failed"
        assert execution.delivered_at is None
        assert execution.last_attempt_at is not None
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_last_attempt_at_updated_on_every_attempt(db_engine, db_session):
    """last_attempt_at is set after each delivery attempt (both success and failure)."""
    received, runner = await start_webhook_receiver(9991)
    try:
        user_id = await _create_test_user(db_session)
        cue = await _create_due_cue(
            db_session, user_id,
            callback_url="http://localhost:9991/webhook",
        )

        await _run_full_cycle(db_engine, cue_id=cue.id)

        session = await _fresh_session(db_engine)
        try:
            result = await session.execute(
                select(Execution).where(Execution.cue_id == cue.id)
            )
            execution = result.scalar_one()
            assert execution.status == "success"
            assert execution.last_attempt_at is not None
            assert execution.delivered_at is not None
            # last_attempt_at should be within a few seconds of delivered_at
            diff = abs((execution.last_attempt_at - execution.delivered_at).total_seconds())
            assert diff < 5
        finally:
            await session.close()
    finally:
        await runner.cleanup()
