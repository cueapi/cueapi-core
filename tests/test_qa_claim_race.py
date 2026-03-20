from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.cue import Cue
from app.models.execution import Execution
from tests.test_poller import _create_due_cue, _create_test_user, _fresh_session
from worker.tasks import _claim_execution


async def _create_execution(db_session, cue_id, status="pending", **kwargs):
    """Create an execution row directly in DB."""
    defaults = dict(
        id=uuid.uuid4(),
        cue_id=cue_id,
        scheduled_for=datetime.now(timezone.utc) - timedelta(seconds=10),
        status=status,
        attempts=0,
    )
    defaults.update(kwargs)
    execution = Execution(**defaults)
    db_session.add(execution)
    await db_session.commit()
    await db_session.refresh(execution)
    return execution


@pytest.mark.asyncio
async def test_claim_from_pending_succeeds(db_engine, db_session):
    """Initial delivery claim succeeds when status is 'pending'."""
    user_id = await _create_test_user(db_session)
    cue = await _create_due_cue(db_session, user_id)
    execution = await _create_execution(db_session, cue.id, status="pending")

    session = await _fresh_session(db_engine)
    try:
        claimed = await _claim_execution(session, str(execution.id), "pending")
        assert claimed is True

        # Verify status changed to 'delivering'
        result = await session.execute(
            select(Execution).where(Execution.id == execution.id)
        )
        updated = result.scalar_one()
        assert updated.status == "delivering"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_claim_from_pending_rejects_delivering(db_engine, db_session):
    """Initial delivery claim fails when execution is already 'delivering'."""
    user_id = await _create_test_user(db_session)
    cue = await _create_due_cue(db_session, user_id)
    execution = await _create_execution(db_session, cue.id, status="delivering")

    session = await _fresh_session(db_engine)
    try:
        claimed = await _claim_execution(session, str(execution.id), "pending")
        assert claimed is False

        # Status should remain 'delivering' (untouched)
        result = await session.execute(
            select(Execution).where(Execution.id == execution.id)
        )
        updated = result.scalar_one()
        assert updated.status == "delivering"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_retry_claim_from_retry_ready_succeeds(db_engine, db_session):
    """Retry claim succeeds when status is 'retry_ready'."""
    user_id = await _create_test_user(db_session)
    cue = await _create_due_cue(db_session, user_id)
    execution = await _create_execution(
        db_session, cue.id, status="retry_ready", attempts=1,
    )

    session = await _fresh_session(db_engine)
    try:
        claimed = await _claim_execution(session, str(execution.id), "retry_ready")
        assert claimed is True

        result = await session.execute(
            select(Execution).where(Execution.id == execution.id)
        )
        updated = result.scalar_one()
        assert updated.status == "delivering"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_retry_claim_rejects_pending(db_engine, db_session):
    """Retry claim fails when execution is still 'pending' (not ready for retry)."""
    user_id = await _create_test_user(db_session)
    cue = await _create_due_cue(db_session, user_id)
    execution = await _create_execution(db_session, cue.id, status="pending")

    session = await _fresh_session(db_engine)
    try:
        claimed = await _claim_execution(session, str(execution.id), "retry_ready")
        assert claimed is False

        result = await session.execute(
            select(Execution).where(Execution.id == execution.id)
        )
        updated = result.scalar_one()
        assert updated.status == "pending"
    finally:
        await session.close()
