"""Tests for Phase 16 — Production Hardening.

Fix 2: Email on key regeneration
Fix 3: GET /v1/workers endpoint
Fix 4: Missed execution status
Fix 5: Distinct error code for rotated keys
Fix 8: fired_count in cue response
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select, update

from app.models.cue import Cue
from app.models.execution import Execution
from app.models.user import User
from app.models.worker import Worker
from app.utils.ids import generate_cue_id, hash_api_key


# ──────────────────────────────────────────────
# Fix 2: Email on key regeneration
# ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_key_regeneration_sends_email(client, auth_headers, redis_client):
    """Regenerating API key should attempt to send email notification."""
    with patch("app.routers.auth_routes._send_key_regeneration_email") as mock_send:
        resp = await client.post("/v1/auth/key/regenerate", headers={**auth_headers, "X-Confirm-Destructive": "true"})
        assert resp.status_code == 200
        mock_send.assert_called_once()
        # Check email is called with user email and a timestamp string
        args = mock_send.call_args[0]
        assert "@" in args[0]  # email
        assert "UTC" in args[1]  # timestamp


@pytest.mark.asyncio
async def test_webhook_secret_regeneration_sends_email(client, auth_headers, redis_client):
    """Regenerating webhook secret should attempt to send email notification."""
    with patch("app.routers.webhook_secret._send_webhook_secret_regeneration_email") as mock_send:
        resp = await client.post("/v1/auth/webhook-secret/regenerate", headers={**auth_headers, "X-Confirm-Destructive": "true"})
        assert resp.status_code == 200
        mock_send.assert_called_once()
        args = mock_send.call_args[0]
        assert "@" in args[0]  # email
        assert "UTC" in args[1]  # timestamp


@pytest.mark.asyncio
async def test_email_contains_timestamp(client, auth_headers, redis_client):
    """Email should contain the regeneration timestamp."""
    with patch("app.routers.auth_routes._send_key_regeneration_email") as mock_send:
        resp = await client.post("/v1/auth/key/regenerate", headers={**auth_headers, "X-Confirm-Destructive": "true"})
        assert resp.status_code == 200
        timestamp = mock_send.call_args[0][1]
        assert "2026" in timestamp or "202" in timestamp  # contains year


@pytest.mark.asyncio
async def test_email_sent_to_correct_user(client, registered_user, redis_client):
    """Email should be sent to the user who regenerated the key."""
    headers = {"Authorization": f"Bearer {registered_user['api_key']}"}
    with patch("app.routers.auth_routes._send_key_regeneration_email") as mock_send:
        resp = await client.post("/v1/auth/key/regenerate", headers={**headers, "X-Confirm-Destructive": "true"})
        assert resp.status_code == 200
        email = mock_send.call_args[0][0]
        assert email == registered_user["email"]


# ──────────────────────────────────────────────
# Fix 3: GET /v1/workers endpoint
# ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_workers_returns_list(client, auth_headers, db_session, registered_user, redis_client):
    """GET /v1/workers should return a list of workers."""
    # First register a worker via heartbeat
    resp = await client.post(
        "/v1/worker/heartbeat",
        headers=auth_headers,
        json={"worker_id": "test-worker-1", "handlers": ["task-a", "task-b"]},
    )
    assert resp.status_code == 200

    resp = await client.get("/v1/workers", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "workers" in data
    assert len(data["workers"]) >= 1
    w = data["workers"][0]
    assert w["worker_id"] == "test-worker-1"
    assert w["handlers"] == ["task-a", "task-b"]
    assert "heartbeat_status" in w
    assert "seconds_since_heartbeat" in w
    assert "registered_since" in w


@pytest.mark.asyncio
async def test_worker_status_active(client, auth_headers, redis_client):
    """Worker with recent heartbeat should be 'active'."""
    await client.post(
        "/v1/worker/heartbeat",
        headers=auth_headers,
        json={"worker_id": "active-worker"},
    )
    resp = await client.get("/v1/workers", headers=auth_headers)
    workers = resp.json()["workers"]
    active = [w for w in workers if w["worker_id"] == "active-worker"]
    assert len(active) == 1
    assert active[0]["heartbeat_status"] == "active"
    assert active[0]["seconds_since_heartbeat"] < 180


@pytest.mark.asyncio
async def test_worker_status_stale(client, auth_headers, db_session, registered_user, redis_client):
    """Worker with heartbeat 3-15 min ago should be 'stale'."""
    await client.post(
        "/v1/worker/heartbeat",
        headers=auth_headers,
        json={"worker_id": "stale-worker"},
    )
    # Manually backdate the heartbeat
    key_hash = hash_api_key(registered_user["api_key"])
    user_result = await db_session.execute(select(User).where(User.api_key_hash == key_hash))
    user = user_result.scalar_one()
    stale_time = datetime.now(timezone.utc) - timedelta(seconds=300)
    await db_session.execute(
        update(Worker)
        .where(Worker.user_id == user.id, Worker.worker_id == "stale-worker")
        .values(last_heartbeat=stale_time)
    )
    await db_session.commit()

    resp = await client.get("/v1/workers", headers=auth_headers)
    workers = resp.json()["workers"]
    stale = [w for w in workers if w["worker_id"] == "stale-worker"]
    assert len(stale) == 1
    assert stale[0]["heartbeat_status"] == "stale"


@pytest.mark.asyncio
async def test_worker_status_dead(client, auth_headers, db_session, registered_user, redis_client):
    """Worker with heartbeat >15 min ago should be 'dead'."""
    await client.post(
        "/v1/worker/heartbeat",
        headers=auth_headers,
        json={"worker_id": "dead-worker"},
    )
    key_hash = hash_api_key(registered_user["api_key"])
    user_result = await db_session.execute(select(User).where(User.api_key_hash == key_hash))
    user = user_result.scalar_one()
    dead_time = datetime.now(timezone.utc) - timedelta(seconds=1200)
    await db_session.execute(
        update(Worker)
        .where(Worker.user_id == user.id, Worker.worker_id == "dead-worker")
        .values(last_heartbeat=dead_time)
    )
    await db_session.commit()

    resp = await client.get("/v1/workers", headers=auth_headers)
    workers = resp.json()["workers"]
    dead = [w for w in workers if w["worker_id"] == "dead-worker"]
    assert len(dead) == 1
    assert dead[0]["heartbeat_status"] == "dead"


@pytest.mark.asyncio
async def test_get_workers_empty_when_none_registered(client, auth_headers, redis_client):
    """GET /v1/workers returns empty list when no workers registered."""
    resp = await client.get("/v1/workers", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["workers"] == []


@pytest.mark.asyncio
async def test_get_workers_auth_required(client, redis_client):
    """GET /v1/workers requires authentication."""
    resp = await client.get("/v1/workers")
    assert resp.status_code == 401


# ──────────────────────────────────────────────
# Fix 4: Missed execution status
# ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unclaimed_execution_becomes_missed(db_session, db_engine):
    """Unclaimed worker execution should get 'missed' status, not 'failed'."""
    from worker.poller import fail_unclaimed_worker_executions

    user_id = str(uuid.uuid4())
    cue_id = generate_cue_id()

    # Create user
    user = User(id=user_id, email=f"test-{uuid.uuid4().hex[:8]}@test.com",
                api_key_hash="test_hash_missed", api_key_prefix="cue_sk_test",
                webhook_secret="whsec_" + "c" * 64, slug=f"test-{uuid.uuid4().hex[:8]}")
    db_session.add(user)

    # Create worker-transport cue
    cue = Cue(
        id=cue_id, user_id=user_id, name="missed-test", status="active",
        schedule_type="recurring", schedule_cron="*/5 * * * *",
        schedule_timezone="UTC", callback_transport="worker",
        callback_method="POST", payload={},
        retry_max_attempts=3, retry_backoff_minutes=[1, 5, 15],
        next_run=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db_session.add(cue)

    # Create old pending execution
    old_time = datetime.now(timezone.utc) - timedelta(minutes=20)
    ex = Execution(
        id=uuid.uuid4(), cue_id=cue_id, scheduled_for=old_time,
        status="pending", attempts=0, created_at=old_time, updated_at=old_time,
    )
    db_session.add(ex)
    await db_session.commit()

    count = await fail_unclaimed_worker_executions(db_engine, unclaimed_timeout=900)
    assert count >= 1

    async with db_engine.begin() as conn:
        result = await conn.execute(
            select(Execution.status, Execution.error_message).where(Execution.id == ex.id)
        )
        row = result.fetchone()
        assert row.status == "missed"
        assert "No worker claimed" in row.error_message


@pytest.mark.asyncio
async def test_missed_execution_in_history(client, auth_headers, db_session, registered_user, redis_client):
    """Missed executions should appear in GET /v1/cues/{id} execution history."""
    key_hash = hash_api_key(registered_user["api_key"])
    user_result = await db_session.execute(select(User).where(User.api_key_hash == key_hash))
    user = user_result.scalar_one()

    cue_id = generate_cue_id()
    cue = Cue(
        id=cue_id, user_id=str(user.id), name="missed-history-test", status="active",
        schedule_type="recurring", schedule_cron="*/5 * * * *",
        schedule_timezone="UTC", callback_transport="worker",
        callback_method="POST", payload={},
        retry_max_attempts=3, retry_backoff_minutes=[1, 5, 15],
        next_run=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db_session.add(cue)

    ex = Execution(
        id=uuid.uuid4(), cue_id=cue_id,
        scheduled_for=datetime.now(timezone.utc) - timedelta(hours=1),
        status="missed",
        error_message="No worker claimed this execution within timeout window",
        attempts=0,
    )
    db_session.add(ex)
    await db_session.commit()

    resp = await client.get(f"/v1/cues/{cue_id}", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["executions"]) == 1
    assert data["executions"][0]["status"] == "missed"
    assert "No worker claimed" in data["executions"][0]["error_message"]


@pytest.mark.asyncio
async def test_missed_status_distinct_from_failed(db_session, db_engine):
    """'missed' and 'failed' are distinct execution statuses."""
    from worker.poller import fail_unclaimed_worker_executions

    user_id = str(uuid.uuid4())
    cue_id = generate_cue_id()

    user = User(id=user_id, email=f"distinct-{uuid.uuid4().hex[:8]}@test.com",
                api_key_hash="test_hash_distinct", api_key_prefix="cue_sk_test",
                webhook_secret="whsec_" + "d" * 64, slug=f"distinct-{uuid.uuid4().hex[:8]}")
    db_session.add(user)

    cue = Cue(
        id=cue_id, user_id=user_id, name="distinct-test", status="active",
        schedule_type="recurring", schedule_cron="*/5 * * * *",
        schedule_timezone="UTC", callback_transport="worker",
        callback_method="POST", payload={},
        retry_max_attempts=3, retry_backoff_minutes=[1, 5, 15],
        next_run=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db_session.add(cue)

    old_time = datetime.now(timezone.utc) - timedelta(minutes=20)
    ex = Execution(
        id=uuid.uuid4(), cue_id=cue_id, scheduled_for=old_time,
        status="pending", attempts=0, created_at=old_time, updated_at=old_time,
    )
    db_session.add(ex)
    await db_session.commit()

    await fail_unclaimed_worker_executions(db_engine, unclaimed_timeout=900)

    async with db_engine.begin() as conn:
        result = await conn.execute(
            select(Execution.status).where(Execution.id == ex.id)
        )
        row = result.fetchone()
        # Must be 'missed' not 'failed'
        assert row.status == "missed"
        assert row.status != "failed"


# ──────────────────────────────────────────────
# Fix 5: Distinct error code for rotated keys
# ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rotated_key_returns_key_rotated_code(client, registered_user, redis_client):
    """Using a rotated key should return error code 'key_rotated'."""
    old_key = registered_user["api_key"]
    old_headers = {"Authorization": f"Bearer {old_key}"}

    # Regenerate key (stores old hash in rotated:* Redis key)
    resp = await client.post("/v1/auth/key/regenerate", headers={**old_headers, "X-Confirm-Destructive": "true"})
    assert resp.status_code == 200

    # Try using old key — should get key_rotated
    resp = await client.get("/v1/cues", headers=old_headers)
    assert resp.status_code == 401
    error = resp.json()["error"]
    assert error["code"] == "key_rotated"
    assert "rotated" in error["message"].lower()


@pytest.mark.asyncio
async def test_invalid_key_returns_invalid_api_key_code(client, redis_client):
    """Using a completely invalid key should return 'invalid_api_key'."""
    headers = {"Authorization": "Bearer cue_sk_00000000000000000000000000000000"}
    resp = await client.get("/v1/cues", headers=headers)
    assert resp.status_code == 401
    error = resp.json()["error"]
    assert error["code"] == "invalid_api_key"


@pytest.mark.asyncio
async def test_rotated_key_expires_after_24h(client, registered_user, redis_client):
    """Rotated key marker should expire — after clearing, error code reverts to invalid_api_key."""
    old_key = registered_user["api_key"]
    old_headers = {"Authorization": f"Bearer {old_key}"}

    # Regenerate
    resp = await client.post("/v1/auth/key/regenerate", headers={**old_headers, "X-Confirm-Destructive": "true"})
    assert resp.status_code == 200

    # Delete the rotated marker from Redis (simulating expiry)
    old_hash = hash_api_key(old_key)
    await redis_client.delete(f"rotated:{old_hash}")

    # Now the old key should get generic invalid_api_key
    resp = await client.get("/v1/cues", headers=old_headers)
    assert resp.status_code == 401
    error = resp.json()["error"]
    assert error["code"] == "invalid_api_key"


@pytest.mark.asyncio
async def test_fresh_invalid_key_not_detected_as_rotated(client, redis_client):
    """A completely new/unknown key should NOT be detected as rotated."""
    headers = {"Authorization": "Bearer cue_sk_aaaaaaaaaaaaaaaaaaaaaaaaaaaa1234"}
    resp = await client.get("/v1/cues", headers=headers)
    assert resp.status_code == 401
    error = resp.json()["error"]
    assert error["code"] == "invalid_api_key"


# ──────────────────────────────────────────────
# Fix 8: fired_count in cue response
# ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fired_count_in_cue_response(client, auth_headers, redis_client):
    """Cue response should include fired_count field."""
    from datetime import datetime, timezone, timedelta
    resp = await client.post(
        "/v1/cues",
        headers=auth_headers,
        json={
            "name": "fired-count-test",
            "schedule": {"type": "once", "at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()},
            "callback": {"url": "https://example.com/webhook"},
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert "fired_count" in data
    assert data["fired_count"] == 0


@pytest.mark.asyncio
async def test_fired_count_increments_on_execution_creation(db_session, db_engine):
    """fired_count should increment when poller creates an execution."""
    from worker.poller import poll_due_cues

    user_id = str(uuid.uuid4())
    cue_id = generate_cue_id()

    user = User(id=user_id, email=f"fired-{uuid.uuid4().hex[:8]}@test.com",
                api_key_hash="test_hash_fired", api_key_prefix="cue_sk_test",
                webhook_secret="whsec_" + "a" * 64, slug=f"fired-{uuid.uuid4().hex[:8]}")
    db_session.add(user)

    cue = Cue(
        id=cue_id, user_id=user_id, name="fired-count-cue", status="active",
        schedule_type="recurring", schedule_cron="*/5 * * * *",
        schedule_timezone="UTC", callback_url="https://example.com/wh",
        callback_method="POST", callback_transport="webhook",
        payload={}, retry_max_attempts=3, retry_backoff_minutes=[1, 5, 15],
        next_run=datetime.now(timezone.utc) - timedelta(minutes=1),
        fired_count=0,
    )
    db_session.add(cue)
    await db_session.commit()

    count = await poll_due_cues(db_engine)
    assert count >= 1

    async with db_engine.begin() as conn:
        result = await conn.execute(select(Cue.fired_count).where(Cue.id == cue_id))
        row = result.fetchone()
        assert row.fired_count == 1


@pytest.mark.asyncio
async def test_fired_count_independent_of_outcome(client, auth_headers, db_session, registered_user, redis_client):
    """fired_count tracks fires regardless of outcome. run_count only on success."""
    key_hash = hash_api_key(registered_user["api_key"])
    user_result = await db_session.execute(select(User).where(User.api_key_hash == key_hash))
    user = user_result.scalar_one()

    cue_id = generate_cue_id()
    cue = Cue(
        id=cue_id, user_id=str(user.id), name="independent-count", status="active",
        schedule_type="recurring", schedule_cron="*/5 * * * *",
        schedule_timezone="UTC", callback_url="https://example.com/wh",
        callback_method="POST", callback_transport="webhook",
        payload={}, retry_max_attempts=3, retry_backoff_minutes=[1, 5, 15],
        next_run=datetime.now(timezone.utc) + timedelta(hours=1),
        fired_count=7, run_count=5,
    )
    db_session.add(cue)
    await db_session.commit()

    resp = await client.get(f"/v1/cues/{cue_id}", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["fired_count"] == 7
    assert data["run_count"] == 5


@pytest.mark.asyncio
async def test_run_count_unchanged_by_fired_count(db_session, db_engine):
    """Incrementing fired_count should not affect run_count."""
    from worker.poller import poll_due_cues

    user_id = str(uuid.uuid4())
    cue_id = generate_cue_id()

    user = User(id=user_id, email=f"runcount-{uuid.uuid4().hex[:8]}@test.com",
                api_key_hash="test_hash_runcount", api_key_prefix="cue_sk_test",
                webhook_secret="whsec_" + "b" * 64, slug=f"runcount-{uuid.uuid4().hex[:8]}")
    db_session.add(user)

    cue = Cue(
        id=cue_id, user_id=user_id, name="runcount-test", status="active",
        schedule_type="recurring", schedule_cron="*/5 * * * *",
        schedule_timezone="UTC", callback_url="https://example.com/wh",
        callback_method="POST", callback_transport="webhook",
        payload={}, retry_max_attempts=3, retry_backoff_minutes=[1, 5, 15],
        next_run=datetime.now(timezone.utc) - timedelta(minutes=1),
        fired_count=0, run_count=3,
    )
    db_session.add(cue)
    await db_session.commit()

    await poll_due_cues(db_engine)

    async with db_engine.begin() as conn:
        result = await conn.execute(select(Cue.fired_count, Cue.run_count).where(Cue.id == cue_id))
        row = result.fetchone()
        assert row.fired_count == 1  # incremented
        assert row.run_count == 3    # unchanged
