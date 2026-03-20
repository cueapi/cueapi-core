"""QA 1.5 — Operational Readiness & Observability tests."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from app.models.device_code import DeviceCode
from app.models.dispatch_outbox import DispatchOutbox
from worker.poller import cleanup_device_codes, cleanup_outbox


# ── Health endpoint ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_includes_metrics(client):
    """Health endpoint returns operational metrics."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "queue" in data
    assert "pending_outbox" in data["queue"]
    assert "stale_executions" in data["queue"]
    assert "pending_retries" in data["queue"]
    assert data["status"] in ("healthy", "degraded")
    assert data["services"]["postgres"] == "ok"
    assert data["services"]["redis"] == "ok"
    assert "timestamp" in data
    assert data["version"] == "1.0.0"


# ── Outbox cleanup ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_outbox_cleanup_removes_old_rows(db_session, db_engine):
    """Dispatched outbox rows older than 7 days are deleted."""
    old_time = datetime.now(timezone.utc) - timedelta(days=10)
    exec_id = uuid.uuid4()

    await db_session.execute(
        DispatchOutbox.__table__.insert().values(
            execution_id=exec_id,
            cue_id="cue_old000001",
            task_type="deliver",
            payload={},
            dispatched=True,
            created_at=old_time,
        )
    )
    await db_session.commit()

    deleted = await cleanup_outbox(db_engine, retention_days=7)
    assert deleted >= 1

    # Verify row is gone
    result = await db_session.execute(
        select(DispatchOutbox).where(DispatchOutbox.execution_id == exec_id)
    )
    assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_outbox_cleanup_keeps_recent(db_session, db_engine):
    """Dispatched outbox rows newer than 7 days are kept."""
    recent_time = datetime.now(timezone.utc) - timedelta(days=1)
    exec_id = uuid.uuid4()

    await db_session.execute(
        DispatchOutbox.__table__.insert().values(
            execution_id=exec_id,
            cue_id="cue_recent0001",
            task_type="deliver",
            payload={},
            dispatched=True,
            created_at=recent_time,
        )
    )
    await db_session.commit()

    await cleanup_outbox(db_engine, retention_days=7)

    # Verify row still exists
    result = await db_session.execute(
        select(DispatchOutbox).where(DispatchOutbox.execution_id == exec_id)
    )
    assert result.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_outbox_cleanup_keeps_undispatched(db_session, db_engine):
    """Undispatched outbox rows are never cleaned up regardless of age."""
    old_time = datetime.now(timezone.utc) - timedelta(days=30)
    exec_id = uuid.uuid4()

    await db_session.execute(
        DispatchOutbox.__table__.insert().values(
            execution_id=exec_id,
            cue_id="cue_undisp001",
            task_type="deliver",
            payload={},
            dispatched=False,
            created_at=old_time,
        )
    )
    await db_session.commit()

    await cleanup_outbox(db_engine, retention_days=7)

    # Verify row still exists
    result = await db_session.execute(
        select(DispatchOutbox).where(DispatchOutbox.execution_id == exec_id)
    )
    assert result.scalar_one_or_none() is not None


# ── Device code cleanup ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_device_code_cleanup(db_session, db_engine):
    """Expired device codes older than 24h are deleted."""
    expired_time = datetime.now(timezone.utc) - timedelta(days=2)
    code_id = uuid.uuid4()

    await db_session.execute(
        DeviceCode.__table__.insert().values(
            id=code_id,
            device_code="EXPR-0001",
            status="expired",
            expires_at=expired_time,
        )
    )
    await db_session.commit()

    deleted = await cleanup_device_codes(db_engine)
    assert deleted >= 1

    # Verify row is gone
    result = await db_session.execute(
        select(DeviceCode).where(DeviceCode.id == code_id)
    )
    assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_device_code_cleanup_keeps_valid(db_session, db_engine):
    """Non-expired device codes are kept."""
    future_time = datetime.now(timezone.utc) + timedelta(hours=1)
    code_id = uuid.uuid4()

    await db_session.execute(
        DeviceCode.__table__.insert().values(
            id=code_id,
            device_code="VALID-001",
            status="pending",
            expires_at=future_time,
        )
    )
    await db_session.commit()

    await cleanup_device_codes(db_engine)

    # Verify row still exists
    result = await db_session.execute(
        select(DeviceCode).where(DeviceCode.id == code_id)
    )
    assert result.scalar_one_or_none() is not None
