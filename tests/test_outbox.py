from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.dispatch_outbox import DispatchOutbox
from app.models.execution import Execution
from tests.test_poller import _create_due_cue, _create_test_user, _fresh_session


@pytest.mark.asyncio
async def test_outbox_row_created_with_execution(db_engine, db_session):
    """Outbox row should be created in the same transaction as execution."""
    from worker.poller import poll_due_cues

    user_id = await _create_test_user(db_session)
    cue = await _create_due_cue(db_session, user_id)

    await poll_due_cues(db_engine, batch_size=500)

    session = await _fresh_session(db_engine)
    try:
        exec_result = await session.execute(
            select(Execution).where(Execution.cue_id == cue.id)
        )
        execution = exec_result.scalar_one_or_none()
        assert execution is not None

        outbox_result = await session.execute(
            select(DispatchOutbox).where(DispatchOutbox.execution_id == execution.id)
        )
        outbox = outbox_result.scalar_one_or_none()
        assert outbox is not None
        assert outbox.dispatched is False
        assert outbox.cue_id == cue.id
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_outbox_dispatch_marks_dispatched(db_engine, db_session):
    """After dispatch, outbox row should have dispatched=True."""
    from worker.poller import poll_due_cues, dispatch_outbox

    user_id = await _create_test_user(db_session)
    cue = await _create_due_cue(db_session, user_id)

    await poll_due_cues(db_engine, batch_size=500)

    # Mock arq_redis
    mock_arq = AsyncMock()
    mock_arq.enqueue_job = AsyncMock(return_value=True)

    count = await dispatch_outbox(db_engine, mock_arq, batch_size=500)
    assert count >= 1

    session = await _fresh_session(db_engine)
    try:
        outbox_result = await session.execute(
            select(DispatchOutbox).where(DispatchOutbox.cue_id == cue.id)
        )
        outbox = outbox_result.scalar_one()
        assert outbox.dispatched is True
    finally:
        await session.close()

    mock_arq.enqueue_job.assert_called()


@pytest.mark.asyncio
async def test_outbox_dispatch_failure_retries_next_cycle(db_engine, db_session):
    """If Redis is down, outbox row stays undispatched with incremented attempts."""
    from worker.poller import poll_due_cues, dispatch_outbox

    user_id = await _create_test_user(db_session)
    cue = await _create_due_cue(db_session, user_id)

    await poll_due_cues(db_engine, batch_size=500)

    # Mock arq_redis that raises on enqueue
    mock_arq = AsyncMock()
    mock_arq.enqueue_job = AsyncMock(side_effect=ConnectionError("Redis down"))

    count = await dispatch_outbox(db_engine, mock_arq, batch_size=500)
    assert count == 0

    session = await _fresh_session(db_engine)
    try:
        outbox_result = await session.execute(
            select(DispatchOutbox).where(DispatchOutbox.cue_id == cue.id)
        )
        outbox = outbox_result.scalar_one()
        assert outbox.dispatched is False
        assert outbox.dispatch_attempts == 1
        assert "Redis down" in outbox.last_dispatch_error
    finally:
        await session.close()
