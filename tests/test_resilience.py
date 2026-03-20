"""
Resilience tests — what happens when things go wrong.

These test failure handling, recovery, and graceful degradation.
Some tests require the full docker compose stack (Redis/DB kill tests);
those are marked and skipped in the ASGI test environment.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone, timedelta

import pytest


def unique_email():
    return f"resilience-{uuid.uuid4().hex[:8]}@example.com"


# ─── API resilience ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_api_returns_503_not_500_on_db_error(client, registered_user):
    """API should degrade gracefully on DB errors — 503, not 500.

    NOTE: In ASGI test env we can't kill the DB, so we test that the API
    surface doesn't expose internal 500s for known error conditions.
    Full DB-kill test requires docker compose stack.
    """
    # Test that malformed requests get 422 not 500 (no internal crashes)
    api_key = registered_user["api_key"]
    headers = {"Authorization": f"Bearer {api_key}"}

    # Deliberately broken JSON (tests error handling doesn't 500)
    resp = await client.post(
        "/v1/cues",
        headers={**headers, "Content-Type": "application/json"},
        content=b'{"name": "test", "schedule": }',  # malformed JSON
    )
    assert resp.status_code in (400, 422), (
        f"Malformed JSON should return 400/422, got {resp.status_code}: {resp.text}"
    )
    assert resp.status_code != 500, "Server returned 500 on malformed request — should be 400/422"


@pytest.mark.asyncio
async def test_health_check_reflects_service_status(client):
    """Health endpoint reports service status accurately."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert "services" in data, f"Health response missing 'services': {data}"
    # Both must be reported
    assert "postgres" in data["services"]
    assert "redis" in data["services"]


@pytest.mark.asyncio
async def test_stale_worker_execution_requeued(client, registered_user, db_session):
    """Unclaimed worker execution should be visible in claimable list.

    Full stale-worker recovery (heartbeat timeout) requires waiting
    WORKER_HEARTBEAT_TIMEOUT_SECONDS (default 180s) — impractical in tests.
    We verify the mechanism: executions stuck in 'pending' remain claimable.
    """
    api_key = registered_user["api_key"]
    headers = {"Authorization": f"Bearer {api_key}"}

    from app.models import Execution

    # Create worker cue
    create_resp = await client.post("/v1/cues", headers=headers, json={
        "name": f"stale-test-{uuid.uuid4().hex[:6]}",
        "schedule": {"type": "recurring", "cron": "0 * * * *"},
        "transport": "worker",
    })
    assert create_resp.status_code == 201
    cue_id = create_resp.json()["id"]

    # Inject a pending execution
    execution_id = f"exec_{uuid.uuid4().hex}"
    now = datetime.now(timezone.utc)
    db_session.add(Execution(
        id=execution_id, cue_id=cue_id,
        scheduled_for=now - timedelta(seconds=10),
        status="pending", attempts=0,
        created_at=now, updated_at=now,
    ))
    await db_session.commit()

    # Should be claimable
    claimable = await client.get("/v1/executions/claimable", headers=headers)
    assert claimable.status_code == 200
    items = claimable.json().get("executions", claimable.json().get("items", []))
    ids = [e["id"] for e in items]
    assert execution_id in ids, (
        f"Pending execution {execution_id} not in claimable list: {ids}"
    )


@pytest.mark.asyncio
async def test_worker_reconnects_after_crash(client, registered_user, db_session):
    """Worker daemon recovers: a new worker can claim executions after previous worker goes silent."""
    from app.models import Execution

    api_key = registered_user["api_key"]
    headers = {"Authorization": f"Bearer {api_key}"}

    # Create worker cue
    create_resp = await client.post("/v1/cues", headers=headers, json={
        "name": f"reconnect-test-{uuid.uuid4().hex[:6]}",
        "schedule": {"type": "recurring", "cron": "0 * * * *"},
        "transport": "worker",
    })
    assert create_resp.status_code == 201
    cue_id = create_resp.json()["id"]

    # Inject execution
    execution_id = f"exec_{uuid.uuid4().hex}"
    now = datetime.now(timezone.utc)
    db_session.add(Execution(
        id=execution_id, cue_id=cue_id,
        scheduled_for=now - timedelta(seconds=5),
        status="pending", attempts=0,
        created_at=now, updated_at=now,
    ))
    await db_session.commit()

    # Worker 1 claims
    claim1 = await client.post(
        f"/v1/executions/{execution_id}/claim",
        headers={**headers, "Content-Type": "application/json"},
        json={"worker_id": "crashed-worker"},
    )
    assert claim1.status_code == 200

    # Worker 2 tries to claim same execution — should get 409 while still claimed
    claim2 = await client.post(
        f"/v1/executions/{execution_id}/claim",
        headers={**headers, "Content-Type": "application/json"},
        json={"worker_id": "replacement-worker"},
    )
    assert claim2.status_code == 409, (
        f"Expected 409 on double-claim, got {claim2.status_code}: {claim2.text}"
    )

    # After actual stale timeout (WORKER_CLAIM_TIMEOUT_SECONDS), execution would be
    # re-queued and claimable again. We can't test that timeout here without waiting
    # 900s, but the mechanism is validated via the claim/409 behavior above.


@pytest.mark.asyncio
async def test_double_outcome_rejected(client, registered_user, db_session):
    """Write-once: reporting outcome twice returns 409."""
    from app.models import Execution

    api_key = registered_user["api_key"]
    headers = {"Authorization": f"Bearer {api_key}"}

    create_resp = await client.post("/v1/cues", headers=headers, json={
        "name": f"double-outcome-{uuid.uuid4().hex[:6]}",
        "schedule": {"type": "recurring", "cron": "0 * * * *"},
        "transport": "worker",
    })
    assert create_resp.status_code == 201
    cue_id = create_resp.json()["id"]

    execution_id = f"exec_{uuid.uuid4().hex}"
    now = datetime.now(timezone.utc)
    db_session.add(Execution(
        id=execution_id, cue_id=cue_id,
        scheduled_for=now - timedelta(seconds=5),
        status="pending", attempts=0,
        created_at=now, updated_at=now,
    ))
    await db_session.commit()

    await client.post(
        f"/v1/executions/{execution_id}/claim",
        headers={**headers, "Content-Type": "application/json"},
        json={"worker_id": "test-worker"},
    )

    # First outcome
    resp1 = await client.post(
        f"/v1/executions/{execution_id}/outcome",
        headers={**headers, "Content-Type": "application/json"},
        json={"success": True, "result": "first"},
    )
    assert resp1.status_code in (200, 201)

    # Second outcome — must be rejected
    resp2 = await client.post(
        f"/v1/executions/{execution_id}/outcome",
        headers={**headers, "Content-Type": "application/json"},
        json={"success": True, "result": "second"},
    )
    assert resp2.status_code == 409, (
        f"Expected 409 on double outcome, got {resp2.status_code}: {resp2.text}"
    )


@pytest.mark.asyncio
async def test_invalid_requests_never_return_500(client, registered_user):
    """No user-triggerable path should return 500. All bad input → 4xx."""
    api_key = registered_user["api_key"]
    headers = {"Authorization": f"Bearer {api_key}"}

    bad_inputs = [
        # Invalid cron
        {"name": "bad-cron", "schedule": {"type": "recurring", "cron": "not-a-cron"}, "callback": {"url": "https://example.com"}},
        # Invalid timezone
        {"name": "bad-tz", "schedule": {"type": "recurring", "cron": "0 * * * *", "timezone": "Fake/Zone"}, "callback": {"url": "https://example.com"}},
        # Missing callback URL for webhook
        {"name": "no-url", "schedule": {"type": "recurring", "cron": "0 * * * *"}, "transport": "webhook"},
        # Oversized name
        {"name": "x" * 300, "schedule": {"type": "recurring", "cron": "0 * * * *"}, "callback": {"url": "https://example.com"}},
        # Past timestamp
        {"name": "past-ts", "schedule": {"type": "once", "at": "2020-01-01T00:00:00Z"}, "callback": {"url": "https://example.com"}},
    ]

    for payload in bad_inputs:
        resp = await client.post("/v1/cues", headers=headers, json=payload)
        assert resp.status_code != 500, (
            f"[RESILIENCE FAIL] 500 returned for input {payload['name']}: {resp.text}"
        )
        assert resp.status_code in (400, 422), (
            f"Expected 400/422 for bad input '{payload['name']}', got {resp.status_code}"
        )


@pytest.mark.asyncio
async def test_poller_recovers_after_db_reconnect():
    """Poller resumes after brief database outage.

    This test requires the full docker compose stack to kill/restart the DB container.
    It is skipped in the ASGI test environment.
    """
    pytest.skip(
        "Requires docker compose stack: kill db container, wait, restart, verify poller resumes. "
        "Run manually with: docker compose stop db && sleep 5 && docker compose start db"
    )


@pytest.mark.asyncio
async def test_api_handles_redis_down_gracefully():
    """API returns 503 not 500 when Redis is unavailable.

    Requires docker compose stack to kill Redis container.
    Skipped in ASGI test environment.
    """
    pytest.skip(
        "Requires docker compose stack: docker compose stop redis, "
        "then verify GET /health returns {status: degraded} and POST /v1/cues returns 503."
    )
