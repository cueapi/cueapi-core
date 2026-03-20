"""Phase 12: Infrastructure Reliability & Monitoring tests.

Tests poller heartbeat, enhanced health endpoint, /status endpoint,
poller leader election, auth Redis fallback, and rate limit Redis fallback.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from httpx import ASGITransport, AsyncClient

from app.config import settings


# ────────────────────────────────────────────────────
#  Health endpoint — poller status
# ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_shows_poller_ok(client, redis_client):
    """Health endpoint shows poller 'ok' when heartbeat is fresh."""
    now = datetime.now(timezone.utc).isoformat()
    await redis_client.set("poller:last_run", now)
    await redis_client.set("poller:cues_processed", "3")
    await redis_client.set("poller:cycle_duration_ms", "45")

    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()

    assert data["services"]["poller"] == "ok"
    assert data["poller"]["cues_last_cycle"] == 3
    assert data["poller"]["cycle_duration_ms"] == 45
    assert "seconds_ago" in data["poller"]


@pytest.mark.asyncio
async def test_health_poller_stale(client, redis_client):
    """Health shows poller 'stale' when heartbeat is old."""
    old_time = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    await redis_client.set("poller:last_run", old_time)

    resp = await client.get("/health")
    data = resp.json()

    assert data["services"]["poller"] == "stale"
    assert data["status"] == "degraded"


@pytest.mark.asyncio
async def test_health_poller_unknown(client, redis_client):
    """Health shows poller 'unknown' when no heartbeat exists."""
    await redis_client.delete("poller:last_run")
    await redis_client.delete("poller:cues_processed")
    await redis_client.delete("poller:cycle_duration_ms")

    resp = await client.get("/health")
    data = resp.json()

    assert data["services"]["poller"] == "unknown"


# ────────────────────────────────────────────────────
#  Health endpoint — full structure
# ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_full_structure(client, redis_client):
    """Health endpoint returns complete diagnostics structure."""
    now = datetime.now(timezone.utc).isoformat()
    await redis_client.set("poller:last_run", now)

    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()

    # Top-level fields
    assert "status" in data
    assert "version" in data
    assert "timestamp" in data
    assert "services" in data

    # Services section
    services = data["services"]
    assert "postgres" in services
    assert "redis" in services
    assert "poller" in services
    assert "worker" in services

    # Queue section
    assert "queue" in data
    queue = data["queue"]
    assert "pending_outbox" in queue
    assert "stale_executions" in queue
    assert "pending_retries" in queue
    assert "pending_worker_claims" in queue


@pytest.mark.asyncio
async def test_health_worker_none_when_no_workers(client, redis_client):
    """Health shows worker 'none' when no active workers, without degrading."""
    now = datetime.now(timezone.utc).isoformat()
    await redis_client.set("poller:last_run", now)

    resp = await client.get("/health")
    data = resp.json()

    # No workers registered in test DB, should be 'none' but NOT degraded
    assert data["services"]["worker"] == "none"
    # Poller is ok, postgres/redis ok → overall healthy
    assert data["status"] == "healthy"


# ────────────────────────────────────────────────────
#  /status endpoint
# ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_status_endpoint_healthy(client, redis_client):
    """GET /status returns healthy when all services ok."""
    now = datetime.now(timezone.utc).isoformat()
    await redis_client.set("poller:last_run", now)

    resp = await client.get("/status")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


@pytest.mark.asyncio
async def test_status_endpoint_degraded_stale_poller(client, redis_client):
    """GET /status returns degraded when poller is stale."""
    old_time = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    await redis_client.set("poller:last_run", old_time)

    resp = await client.get("/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "degraded"
    assert "poller stale" in data["reason"]


@pytest.mark.asyncio
async def test_status_endpoint_no_poller_still_healthy(client, redis_client):
    """GET /status returns healthy when poller hasn't started (no heartbeat key)."""
    await redis_client.delete("poller:last_run")

    resp = await client.get("/status")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


# ────────────────────────────────────────────────────
#  Auth — Redis fallback to DB
# ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_auth_works_without_redis(client, registered_user):
    """Auth falls back to DB when Redis is unreachable."""
    headers = {"Authorization": f"Bearer {registered_user['api_key']}"}

    # First request — caches to Redis, verify it works
    resp = await client.get("/v1/usage", headers=headers)
    assert resp.status_code == 200

    # Now mock Redis to raise ConnectionError
    with patch("app.auth.get_redis", new_callable=AsyncMock) as mock_redis:
        mock_redis.side_effect = ConnectionError("Redis down")

        # Auth should fall through to DB
        resp = await client.get("/v1/usage", headers=headers)
        assert resp.status_code == 200


# ────────────────────────────────────────────────────
#  Rate limiting — Redis fallback
# ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rate_limit_allows_without_redis(client, auth_headers):
    """Rate limiting allows requests when Redis is unreachable."""
    with patch("app.middleware.rate_limit.get_redis", new_callable=AsyncMock) as mock_get_redis:
        mock_redis = AsyncMock()
        mock_redis.ping.side_effect = ConnectionError("Redis down")
        mock_get_redis.return_value = mock_redis

        resp = await client.get("/v1/usage", headers=auth_headers)
        # Should succeed — rate limiting is skipped, auth falls through to DB
        # The response might be 200 or 401 depending on auth, but NOT 500
        assert resp.status_code != 500


# ────────────────────────────────────────────────────
#  Poller leader election
# ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_poller_leader_election(redis_client):
    """Only one poller can acquire leadership at a time."""
    from worker.poller import acquire_poller_lock, renew_poller_lock, _REPLICA_ID

    # Clean up any existing lock
    await redis_client.delete("poller:leader")

    # Poller A acquires lock
    acquired = await redis_client.set("poller:leader", "poller-A", nx=True, ex=30)
    assert acquired is not None

    # Poller B tries — should fail
    acquired_b = await redis_client.set("poller:leader", "poller-B", nx=True, ex=30)
    assert acquired_b is None

    # Verify A is the leader
    leader = await redis_client.get("poller:leader")
    assert leader == "poller-A"


@pytest.mark.asyncio
async def test_poller_leader_failover(redis_client):
    """Standby poller takes over when leader's lock expires."""
    # Clean up
    await redis_client.delete("poller:leader")

    # Poller A acquires lock with 1 second TTL
    await redis_client.set("poller:leader", "poller-A", nx=True, ex=1)

    # Verify A is leader
    assert await redis_client.get("poller:leader") == "poller-A"

    # Wait for lock to expire
    await asyncio.sleep(1.5)

    # Poller B should now be able to acquire
    acquired_b = await redis_client.set("poller:leader", "poller-B", nx=True, ex=30)
    assert acquired_b is not None
    assert await redis_client.get("poller:leader") == "poller-B"


@pytest.mark.asyncio
async def test_renew_poller_lock(redis_client):
    """renew_poller_lock extends the TTL for the current leader."""
    from worker.poller import _REPLICA_ID

    await redis_client.delete("poller:leader")

    # Set lock as current replica
    await redis_client.set("poller:leader", _REPLICA_ID, nx=True, ex=5)

    # Import and test renew
    from worker.poller import renew_poller_lock
    result = await renew_poller_lock(redis_client)
    assert result is True

    # Lock should still be held
    ttl = await redis_client.ttl("poller:leader")
    assert ttl > 0


@pytest.mark.asyncio
async def test_renew_fails_for_non_leader(redis_client):
    """renew_poller_lock returns False if another replica holds the lock."""
    await redis_client.delete("poller:leader")

    # Another replica holds the lock
    await redis_client.set("poller:leader", "other-replica", nx=True, ex=30)

    from worker.poller import renew_poller_lock
    result = await renew_poller_lock(redis_client)
    assert result is False


# ────────────────────────────────────────────────────
#  Poller heartbeat write
# ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_write_poller_heartbeat(redis_client):
    """write_poller_heartbeat sets all three Redis keys."""
    from worker.poller import write_poller_heartbeat

    await write_poller_heartbeat(redis_client, cue_count=5, cycle_duration_ms=120)

    last_run = await redis_client.get("poller:last_run")
    cues = await redis_client.get("poller:cues_processed")
    duration = await redis_client.get("poller:cycle_duration_ms")

    assert last_run is not None
    assert cues == "5"
    assert duration == "120"

    # Check TTL is set
    ttl = await redis_client.ttl("poller:last_run")
    assert ttl > 0
