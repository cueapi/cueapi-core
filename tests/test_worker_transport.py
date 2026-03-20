"""Tests for Phase 10 — Worker Transport (Part A: Server-Side).

22 tests covering:
- Worker cue creation (no URL, with URL, webhook requires URL, warning, no warning, transport in callback)
- Claimable endpoint (returns worker execs, excludes webhook, task filter)
- Claim endpoint (success, sets status, already claimed 409, wrong user 409, no worker_id)
- Heartbeat (creates worker, updates existing)
- Poller (skips outbox, fail unclaimed, recover stale claim)
- Worker outcome lifecycle (success sets status+run_count, failure sets status, one-time cue completes)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient

from sqlalchemy import select

from app.models.cue import Cue
from app.models.execution import Execution
from app.models.user import User
from app.models.worker import Worker
from app.utils.ids import generate_cue_id


async def _get_user_id(db_session, registered_user):
    """Look up user UUID from DB by email."""
    result = await db_session.execute(
        select(User.id).where(User.email == registered_user["email"])
    )
    return result.scalar_one()


# --- Helper to create a worker cue directly in DB ---


async def _create_worker_cue(db_session, user_id, name="test-worker-cue", payload=None):
    """Insert a worker-transport cue directly into DB (bypasses API validation for time)."""
    cue_id = generate_cue_id()
    now = datetime.now(timezone.utc)
    cue = Cue(
        id=cue_id,
        user_id=user_id,
        name=name,
        status="active",
        schedule_type="recurring",
        schedule_cron="*/5 * * * *",
        schedule_timezone="UTC",
        callback_url=None,
        callback_method="POST",
        callback_transport="worker",
        payload=payload or {"task": "summarize"},
        retry_max_attempts=3,
        retry_backoff_minutes=[1, 5, 15],
        next_run=now + timedelta(hours=1),
    )
    db_session.add(cue)
    await db_session.commit()
    await db_session.refresh(cue)
    return cue


async def _create_execution(db_session, cue_id, status="pending", claimed_by=None, claimed_at=None, created_at=None):
    """Insert an execution directly into DB."""
    exec_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    ex = Execution(
        id=exec_id,
        cue_id=cue_id,
        scheduled_for=now,
        status=status,
        claimed_by_worker=claimed_by,
        claimed_at=claimed_at,
        started_at=claimed_at,
        created_at=created_at or now,
    )
    db_session.add(ex)
    await db_session.commit()
    return ex


# ====================
# 1-4: Worker Cue Creation
# ====================


@pytest.mark.asyncio
async def test_create_worker_cue_no_url(client: AsyncClient, auth_headers):
    """Worker cue can be created without a callback URL."""
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    response = await client.post(
        "/v1/cues",
        json={
            "name": "Worker Cue",
            "schedule": {"type": "once", "at": future},
            "transport": "worker",
            "payload": {"task": "summarize"},
        },
        headers=auth_headers,
    )
    assert response.status_code == 201
    data = response.json()
    assert data["transport"] == "worker"
    assert data["callback"]["url"] is None


@pytest.mark.asyncio
async def test_create_webhook_cue_requires_url(client: AsyncClient, auth_headers):
    """Webhook cue without URL should fail validation."""
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    response = await client.post(
        "/v1/cues",
        json={
            "name": "No URL Cue",
            "schedule": {"type": "once", "at": future},
            "transport": "webhook",
        },
        headers=auth_headers,
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_worker_cue_no_workers_warning(client: AsyncClient, auth_headers):
    """Worker cue created without active workers should include warning."""
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    response = await client.post(
        "/v1/cues",
        json={
            "name": "Worker Cue Warning",
            "schedule": {"type": "once", "at": future},
            "transport": "worker",
            "payload": {"task": "test"},
        },
        headers=auth_headers,
    )
    assert response.status_code == 201
    data = response.json()
    assert data["warning"] is not None
    assert "No active workers" in data["warning"]


@pytest.mark.asyncio
async def test_create_worker_cue_with_active_worker_no_warning(
    client: AsyncClient, auth_headers, db_session, registered_user
):
    """Worker cue created with active worker should NOT include warning."""
    # Create an active worker via heartbeat
    await client.post(
        "/v1/worker/heartbeat",
        json={"worker_id": "test-host-1", "handlers": ["summarize"]},
        headers=auth_headers,
    )

    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    response = await client.post(
        "/v1/cues",
        json={
            "name": "Worker Cue No Warning",
            "schedule": {"type": "once", "at": future},
            "transport": "worker",
            "payload": {"task": "summarize"},
        },
        headers=auth_headers,
    )
    assert response.status_code == 201
    data = response.json()
    assert data["warning"] is None


# ====================
# 5-7: Claimable Endpoint
# ====================


@pytest.mark.asyncio
async def test_claimable_returns_pending_worker_executions(
    client: AsyncClient, auth_headers, db_session, registered_user
):
    """GET /v1/executions/claimable returns pending worker-transport executions."""
    user_id = await _get_user_id(db_session, registered_user)
    cue = await _create_worker_cue(db_session, user_id)
    await _create_execution(db_session, cue.id, status="pending")

    response = await client.get("/v1/executions/claimable", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert len(data["executions"]) >= 1
    assert data["executions"][0]["task"] == "summarize"


@pytest.mark.asyncio
async def test_claimable_excludes_webhook_executions(
    client: AsyncClient, auth_headers, db_session, registered_user
):
    """GET /v1/executions/claimable should NOT return webhook-transport executions."""
    # Create a webhook cue + execution
    user_id = await _get_user_id(db_session, registered_user)
    cue_id = generate_cue_id()
    now = datetime.now(timezone.utc)
    webhook_cue = Cue(
        id=cue_id,
        user_id=user_id,
        name="webhook-cue",
        status="active",
        schedule_type="recurring",
        schedule_cron="*/5 * * * *",
        schedule_timezone="UTC",
        callback_url="https://example.com/hook",
        callback_method="POST",
        callback_transport="webhook",
        payload={"task": "webhook-task"},
        retry_max_attempts=3,
        retry_backoff_minutes=[1, 5, 15],
        next_run=now + timedelta(hours=1),
    )
    db_session.add(webhook_cue)
    await db_session.commit()
    await _create_execution(db_session, cue_id, status="pending")

    response = await client.get("/v1/executions/claimable", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    cue_ids = [e["cue_id"] for e in data["executions"]]
    assert cue_id not in cue_ids


@pytest.mark.asyncio
async def test_claimable_filters_by_task(
    client: AsyncClient, auth_headers, db_session, registered_user
):
    """GET /v1/executions/claimable?task= filters by payload.task."""
    user_id = await _get_user_id(db_session, registered_user)
    cue1 = await _create_worker_cue(db_session, user_id, name="cue-a", payload={"task": "alpha"})
    cue2 = await _create_worker_cue(db_session, user_id, name="cue-b", payload={"task": "beta"})
    await _create_execution(db_session, cue1.id, status="pending")
    await _create_execution(db_session, cue2.id, status="pending")

    response = await client.get(
        "/v1/executions/claimable?task=alpha", headers=auth_headers
    )
    assert response.status_code == 200
    data = response.json()
    tasks = [e["task"] for e in data["executions"]]
    assert "alpha" in tasks
    assert "beta" not in tasks


# ====================
# 8-11: Claim Endpoint
# ====================


@pytest.mark.asyncio
async def test_claim_execution_success(
    client: AsyncClient, auth_headers, db_session, registered_user
):
    """POST /v1/executions/{id}/claim returns claimed=true."""
    user_id = await _get_user_id(db_session, registered_user)
    cue = await _create_worker_cue(db_session, user_id)
    ex = await _create_execution(db_session, cue.id, status="pending")

    response = await client.post(
        f"/v1/executions/{ex.id}/claim",
        json={"worker_id": "my-worker-1"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["claimed"] is True
    assert data["execution_id"] == str(ex.id)
    assert data["lease_seconds"] == 900


@pytest.mark.asyncio
async def test_claim_sets_delivering_status(
    client: AsyncClient, auth_headers, db_session, registered_user
):
    """After claim, execution should be in 'delivering' status with claim fields set."""
    user_id = await _get_user_id(db_session, registered_user)
    cue = await _create_worker_cue(db_session, user_id)
    ex = await _create_execution(db_session, cue.id, status="pending")

    await client.post(
        f"/v1/executions/{ex.id}/claim",
        json={"worker_id": "claimer-1"},
        headers=auth_headers,
    )

    # Verify in DB via raw column select (avoids greenlet issues with cached ORM objects)
    result = await db_session.execute(
        select(
            Execution.status,
            Execution.claimed_by_worker,
            Execution.claimed_at,
            Execution.attempts,
        ).where(Execution.id == ex.id)
    )
    row = result.fetchone()
    assert row.status == "delivering"
    assert row.claimed_by_worker == "claimer-1"
    assert row.claimed_at is not None
    assert row.attempts == 1


@pytest.mark.asyncio
async def test_claim_already_claimed_409(
    client: AsyncClient, auth_headers, db_session, registered_user
):
    """Claiming an already-claimed execution returns 409."""
    user_id = await _get_user_id(db_session, registered_user)
    cue = await _create_worker_cue(db_session, user_id)
    ex = await _create_execution(db_session, cue.id, status="pending")

    # First claim succeeds
    r1 = await client.post(
        f"/v1/executions/{ex.id}/claim",
        json={"worker_id": "worker-a"},
        headers=auth_headers,
    )
    assert r1.status_code == 200

    # Second claim fails
    r2 = await client.post(
        f"/v1/executions/{ex.id}/claim",
        json={"worker_id": "worker-b"},
        headers=auth_headers,
    )
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_claim_wrong_user_409(
    client: AsyncClient, auth_headers, other_auth_headers, db_session, registered_user
):
    """Claiming another user's execution returns 409."""
    user_id = await _get_user_id(db_session, registered_user)
    cue = await _create_worker_cue(db_session, user_id)
    ex = await _create_execution(db_session, cue.id, status="pending")

    response = await client.post(
        f"/v1/executions/{ex.id}/claim",
        json={"worker_id": "other-worker"},
        headers=other_auth_headers,
    )
    assert response.status_code == 409


# ====================
# 12-13: Heartbeat
# ====================


@pytest.mark.asyncio
async def test_heartbeat_creates_worker(
    client: AsyncClient, auth_headers, db_session
):
    """POST /v1/worker/heartbeat creates a new worker record."""
    response = await client.post(
        "/v1/worker/heartbeat",
        json={"worker_id": "host-abc", "handlers": ["summarize", "review"]},
        headers=auth_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["acknowledged"] is True
    assert "server_time" in data


@pytest.mark.asyncio
async def test_heartbeat_updates_existing(
    client: AsyncClient, auth_headers, db_session
):
    """Second heartbeat updates handlers and timestamp, not duplicate row."""
    await client.post(
        "/v1/worker/heartbeat",
        json={"worker_id": "host-xyz", "handlers": ["old"]},
        headers=auth_headers,
    )
    r2 = await client.post(
        "/v1/worker/heartbeat",
        json={"worker_id": "host-xyz", "handlers": ["new1", "new2"]},
        headers=auth_headers,
    )
    assert r2.status_code == 200

    # Verify only one row exists
    from sqlalchemy import select, func
    result = await db_session.execute(
        select(func.count()).select_from(Worker).where(Worker.worker_id == "host-xyz")
    )
    count = result.scalar()
    assert count == 1

    # Verify handlers updated
    result2 = await db_session.execute(
        select(Worker).where(Worker.worker_id == "host-xyz")
    )
    worker = result2.scalar_one()
    assert worker.handlers == ["new1", "new2"]


# ====================
# 14: Poller skips outbox for worker cue
# ====================


@pytest.mark.asyncio
async def test_poller_skips_outbox_for_worker_cue(db_session, db_engine, registered_user):
    """Poller should NOT create outbox row for worker-transport cue."""
    from worker.poller import poll_due_cues
    from sqlalchemy import func

    user_id = await _get_user_id(db_session, registered_user)

    # Create a worker cue with next_run in the past so poller picks it up
    cue_id = generate_cue_id()
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    cue = Cue(
        id=cue_id,
        user_id=user_id,
        name="worker-poller-test",
        status="active",
        schedule_type="once",
        schedule_cron=None,
        schedule_timezone="UTC",
        callback_url=None,
        callback_method="POST",
        callback_transport="worker",
        payload={"task": "test"},
        retry_max_attempts=3,
        retry_backoff_minutes=[1, 5, 15],
        next_run=past,
    )
    db_session.add(cue)
    await db_session.commit()

    # Run poller
    processed = await poll_due_cues(db_engine, batch_size=100)
    assert processed >= 1

    # Verify execution created
    async with db_engine.begin() as conn:
        exec_result = await conn.execute(
            select(func.count()).select_from(Execution).where(Execution.cue_id == cue_id)
        )
        exec_count = exec_result.scalar()
        assert exec_count >= 1

        # Verify NO outbox row
        from app.models.dispatch_outbox import DispatchOutbox
        outbox_result = await conn.execute(
            select(func.count()).select_from(DispatchOutbox).where(DispatchOutbox.cue_id == cue_id)
        )
        outbox_count = outbox_result.scalar()
        assert outbox_count == 0


# ====================
# 15: Fail unclaimed worker execution
# ====================


@pytest.mark.asyncio
async def test_fail_unclaimed_worker_execution(db_session, db_engine, registered_user):
    """Pending worker execution older than timeout should be marked missed."""
    from worker.poller import fail_unclaimed_worker_executions
    from sqlalchemy import select

    user_id = await _get_user_id(db_session, registered_user)
    cue = await _create_worker_cue(db_session, user_id)

    # Create execution with old created_at (>15 min ago)
    old_time = datetime.now(timezone.utc) - timedelta(minutes=20)
    ex = await _create_execution(db_session, cue.id, status="pending", created_at=old_time)

    count = await fail_unclaimed_worker_executions(db_engine, unclaimed_timeout=900)
    assert count >= 1

    # Verify execution is failed
    async with db_engine.begin() as conn:
        result = await conn.execute(
            select(Execution.status, Execution.error_message).where(Execution.id == ex.id)
        )
        row = result.fetchone()
        assert row.status == "missed"
        assert "No worker claimed" in row.error_message


# ====================
# 16: Recover stale worker claim
# ====================


@pytest.mark.asyncio
async def test_recover_stale_worker_claim(db_session, db_engine, registered_user):
    """Stale worker claim (stale heartbeat + expired lease) should be reset to pending."""
    from worker.poller import recover_stale_worker_claims
    from sqlalchemy import select

    user_id = await _get_user_id(db_session, registered_user)
    cue = await _create_worker_cue(db_session, user_id)

    # Create a stale worker (heartbeat was 10 minutes ago)
    stale_time = datetime.now(timezone.utc) - timedelta(minutes=10)
    worker = Worker(
        user_id=user_id,
        worker_id="stale-worker",
        handlers=["summarize"],
        last_heartbeat=stale_time,
    )
    db_session.add(worker)
    await db_session.commit()

    # Create a delivering execution claimed by this stale worker 20 min ago
    old_claim = datetime.now(timezone.utc) - timedelta(minutes=20)
    ex = await _create_execution(
        db_session, cue.id,
        status="delivering",
        claimed_by="stale-worker",
        claimed_at=old_claim,
    )

    count = await recover_stale_worker_claims(
        db_engine,
        heartbeat_timeout=180,  # 3 min — worker is stale (10 min ago)
        claim_timeout=900,      # 15 min — claim is expired (20 min ago)
    )
    assert count >= 1

    # Verify execution reset to pending
    async with db_engine.begin() as conn:
        result = await conn.execute(
            select(Execution.status, Execution.claimed_by_worker).where(Execution.id == ex.id)
        )
        row = result.fetchone()
        assert row.status == "pending"
        assert row.claimed_by_worker is None


# ====================
# 17: Worker cue with URL keeps transport
# ====================


@pytest.mark.asyncio
async def test_create_worker_cue_with_url(client: AsyncClient, auth_headers):
    """Worker cue with both URL and transport='worker' should keep transport as 'worker'."""
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    response = await client.post(
        "/v1/cues",
        json={
            "name": "Worker With URL",
            "schedule": {"type": "once", "at": future},
            "transport": "worker",
            "callback": {"url": "https://example.com/fallback"},
            "payload": {"task": "summarize"},
        },
        headers=auth_headers,
    )
    assert response.status_code == 201
    data = response.json()
    assert data["transport"] == "worker"
    assert "example.com" in data["callback"]["url"]


# ====================
# 18: Claim without worker_id is rejected (worker_id is required)
# ====================


@pytest.mark.asyncio
async def test_claim_without_worker_id(
    client: AsyncClient, auth_headers, db_session, registered_user
):
    """POST /v1/executions/{id}/claim with empty body must return 422 (worker_id required)."""
    user_id = await _get_user_id(db_session, registered_user)
    cue = await _create_worker_cue(db_session, user_id)
    ex = await _create_execution(db_session, cue.id, status="pending")

    response = await client.post(
        f"/v1/executions/{ex.id}/claim",
        json={},
        headers=auth_headers,
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"


# ====================
# 19: Transport nested inside callback
# ====================


@pytest.mark.asyncio
async def test_create_worker_cue_transport_inside_callback_rejected(client: AsyncClient, auth_headers):
    """Worker cue with transport inside callback object should be rejected with 422."""
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    response = await client.post(
        "/v1/cues",
        json={
            "name": "worker-transport-test",
            "schedule": {"type": "once", "at": future},
            "callback": {"transport": "worker"},
            "payload": {"kind": "scheduled_task", "task": "post-opinion"},
        },
        headers=auth_headers,
    )
    assert response.status_code == 422
    body = response.json()
    assert "error" in body


# ====================
# 20-22: Worker outcome lifecycle
# ====================


@pytest.mark.asyncio
async def test_worker_outcome_success_sets_status_and_run_count(
    client: AsyncClient, auth_headers, db_session, registered_user
):
    """Reporting success outcome on worker execution sets status=success and increments run_count."""
    user_id = await _get_user_id(db_session, registered_user)
    cue = await _create_worker_cue(db_session, user_id)
    ex = await _create_execution(db_session, cue.id, status="delivering")

    response = await client.post(
        f"/v1/executions/{ex.id}/outcome",
        json={"success": True, "result": "Handler completed"},
        headers=auth_headers,
    )
    assert response.status_code == 200

    # Verify execution status
    result = await db_session.execute(
        select(Execution.status, Execution.delivered_at).where(Execution.id == ex.id)
    )
    row = result.fetchone()
    assert row.status == "success"
    assert row.delivered_at is not None

    # Verify cue run_count and last_run
    cue_result = await db_session.execute(
        select(Cue.run_count, Cue.last_run).where(Cue.id == cue.id)
    )
    cue_row = cue_result.fetchone()
    assert cue_row.run_count == 1
    assert cue_row.last_run is not None


@pytest.mark.asyncio
async def test_worker_outcome_failure_sets_status(
    client: AsyncClient, auth_headers, db_session, registered_user
):
    """Reporting failure outcome on worker execution sets status=failed."""
    user_id = await _get_user_id(db_session, registered_user)
    cue = await _create_worker_cue(db_session, user_id)
    ex = await _create_execution(db_session, cue.id, status="delivering")

    response = await client.post(
        f"/v1/executions/{ex.id}/outcome",
        json={"success": False, "error": "Handler crashed"},
        headers=auth_headers,
    )
    assert response.status_code == 200

    result = await db_session.execute(
        select(Execution.status, Execution.error_message).where(Execution.id == ex.id)
    )
    row = result.fetchone()
    assert row.status == "failed"
    assert row.error_message == "Handler crashed"


@pytest.mark.asyncio
async def test_worker_outcome_success_completes_onetime_cue(
    client: AsyncClient, auth_headers, db_session, registered_user
):
    """Success outcome on one-time worker cue should mark cue as completed."""
    user_id = await _get_user_id(db_session, registered_user)

    # Create a one-time worker cue
    cue_id = generate_cue_id()
    now = datetime.now(timezone.utc)
    cue = Cue(
        id=cue_id,
        user_id=user_id,
        name="onetime-worker",
        status="active",
        schedule_type="once",
        schedule_at=now,
        schedule_timezone="UTC",
        callback_url=None,
        callback_method="POST",
        callback_transport="worker",
        payload={"task": "summarize"},
        retry_max_attempts=3,
        retry_backoff_minutes=[1, 5, 15],
        next_run=now,
    )
    db_session.add(cue)
    await db_session.commit()
    await db_session.refresh(cue)

    ex = await _create_execution(db_session, cue_id, status="delivering")

    response = await client.post(
        f"/v1/executions/{ex.id}/outcome",
        json={"success": True, "result": "Done"},
        headers=auth_headers,
    )
    assert response.status_code == 200

    # Verify cue is now completed
    cue_result = await db_session.execute(
        select(Cue.status).where(Cue.id == cue_id)
    )
    cue_row = cue_result.fetchone()
    assert cue_row.status == "completed"
