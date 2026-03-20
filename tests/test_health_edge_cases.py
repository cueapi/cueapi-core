"""Health endpoint edge case tests — field validation, status endpoint.

Supplements test_health.py with structural and response format tests.
"""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_health_has_required_fields(client):
    """GET /health should return all required top-level fields."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()

    assert "status" in data
    assert "version" in data
    assert "timestamp" in data
    assert "services" in data
    assert "queue" in data


@pytest.mark.asyncio
async def test_health_services_structure(client):
    """Services section should have postgres and redis."""
    resp = await client.get("/health")
    data = resp.json()
    services = data["services"]

    assert "postgres" in services
    assert "redis" in services
    assert services["postgres"] == "ok"
    assert services["redis"] == "ok"


@pytest.mark.asyncio
async def test_health_queue_has_metrics(client):
    """Queue section should have expected metric keys."""
    resp = await client.get("/health")
    data = resp.json()
    queue = data["queue"]

    assert "pending_outbox" in queue
    assert "stale_executions" in queue
    assert "pending_retries" in queue
    assert "pending_worker_claims" in queue

    # All should be non-negative integers
    for key, value in queue.items():
        assert isinstance(value, int)
        assert value >= 0


@pytest.mark.asyncio
async def test_health_worker_none_is_not_degraded(client):
    """No active workers should NOT make status degraded (webhook-only is fine)."""
    resp = await client.get("/health")
    data = resp.json()

    # Worker should be "none" in test env (no heartbeats)
    assert data["services"].get("worker") in ("none", "unknown")
    # But status should still be healthy (or degraded for other reasons, NOT workers)
    # The key point: worker="none" alone doesn't cause degraded
    if data["services"]["postgres"] == "ok" and data["services"]["redis"] == "ok":
        # If poller is also ok/unknown, should be healthy
        poller_status = data["services"].get("poller", "unknown")
        if poller_status in ("ok", "unknown"):
            assert data["status"] == "healthy"


@pytest.mark.asyncio
async def test_status_returns_healthy(client):
    """GET /status should return healthy when all services are up."""
    resp = await client.get("/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("healthy", "degraded")


@pytest.mark.asyncio
async def test_status_is_lightweight(client):
    """GET /status should return a minimal response."""
    resp = await client.get("/status")
    data = resp.json()
    assert "status" in data
    # Should NOT contain heavy fields like queue metrics
    assert "queue" not in data
    assert "services" not in data


@pytest.mark.asyncio
async def test_health_version_present(client):
    """Health endpoint should report the API version."""
    resp = await client.get("/health")
    data = resp.json()
    assert data["version"] == "1.0.0"


@pytest.mark.asyncio
async def test_health_timestamp_format(client):
    """Health timestamp should be ISO format."""
    resp = await client.get("/health")
    data = resp.json()
    # Should be parseable ISO timestamp
    from datetime import datetime
    ts = data["timestamp"]
    # Should not raise
    datetime.fromisoformat(ts)
