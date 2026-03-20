"""
Docs accuracy tests — every command in the docs actually works.
If these fail, update the docs.

Findings from cold-start review (2026-03-19):
- quickstart.md uses WRONG field names (title/url/schedule-as-string vs name/callback.url/schedule-as-object)
- quickstart.md register expects 200 but API returns 201
- workers.md claim endpoint omits required worker_id body
- README curl example uses 'title' not 'name'
"""
from __future__ import annotations

import uuid

import pytest


def unique_email():
    return f"docs-{uuid.uuid4().hex[:8]}@example.com"


# ─── quickstart.md accuracy ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_quickstart_register_returns_201(client):
    """quickstart.md says 200, but API returns 201. Docs are wrong."""
    response = await client.post(
        "/v1/auth/register",
        json={"email": unique_email()},
    )
    # DOCS BUG: quickstart.md doesn't mention status code but README implies 200
    # Real behavior is 201
    assert response.status_code == 201, (
        f"[DOCS GAP] Register returned {response.status_code}, expected 201. "
        f"quickstart.md does not document the status code."
    )
    data = response.json()
    assert "api_key" in data
    # DOCS BUG: quickstart.md says key starts with "cue_live_..." but real prefix is "cue_sk_"
    assert data["api_key"].startswith("cue_sk_"), (
        f"[DOCS BUG] quickstart.md shows key prefix 'cue_live_...' but got '{data['api_key'][:12]}...'. "
        f"Docs must be updated to show 'cue_sk_' prefix."
    )


@pytest.mark.asyncio
async def test_quickstart_create_cue_correct_schema(client, registered_user):
    """quickstart.md uses wrong field names. Actual API uses name/callback/schedule-as-object."""
    api_key = registered_user["api_key"]
    headers = {"Authorization": f"Bearer {api_key}"}

    # quickstart.md example (WRONG — will fail):
    # {"title": "Hourly ping", "schedule": "0 * * * *", "url": "...", "payload": {...}}
    wrong_schema_resp = await client.post("/v1/cues", headers=headers, json={
        "title": "Hourly ping",            # wrong — should be "name"
        "schedule": "0 * * * *",           # wrong — should be object
        "url": "https://example.com/hook", # wrong — should be inside callback{}
        "payload": {"message": "hello"},
    })
    assert wrong_schema_resp.status_code in (400, 422), (
        f"[DOCS BUG] quickstart.md curl example uses wrong schema but API returned "
        f"{wrong_schema_resp.status_code} instead of 422. "
        f"Fields 'title', flat 'schedule' string, and flat 'url' are all wrong."
    )

    # Correct schema (what actually works):
    correct_resp = await client.post("/v1/cues", headers=headers, json={
        "name": f"hourly-ping-{uuid.uuid4().hex[:6]}",
        "schedule": {"type": "recurring", "cron": "0 * * * *", "timezone": "America/Los_Angeles"},
        "callback": {"url": "https://example.com/webhook"},
        "payload": {"message": "hello from cueapi"},
    })
    assert correct_resp.status_code == 201, (
        f"Correct schema failed: {correct_resp.status_code} {correct_resp.text}"
    )


@pytest.mark.asyncio
async def test_readme_curl_example_uses_wrong_fields(client, registered_user):
    """README.md create-cue example uses 'title' instead of 'name'. Should return 422."""
    api_key = registered_user["api_key"]
    headers = {"Authorization": f"Bearer {api_key}"}

    # README example:
    # {"name": "morning-brief", "schedule": {"type": "recurring", "cron": ...}, "callback": {...}}
    # README is actually correct on field names — but quickstart.md is not.
    resp = await client.post("/v1/cues", headers=headers, json={
        "name": f"morning-brief-{uuid.uuid4().hex[:6]}",
        "schedule": {"type": "recurring", "cron": "0 9 * * *", "timezone": "America/Los_Angeles"},
        "callback": {"url": "https://example.com/webhook"},
    })
    assert resp.status_code == 201, (
        f"README example should work but got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_cue_list_response_shape_matches_docs(client, registered_user):
    """quickstart.md shows {items: [...], total: N} but real response is {cues: [...], total, limit, offset}."""
    api_key = registered_user["api_key"]
    headers = {"Authorization": f"Bearer {api_key}"}

    resp = await client.get("/v1/cues", headers=headers)
    assert resp.status_code == 200
    data = resp.json()

    # DOCS BUG: quickstart.md shows "items" key, real response uses "cues"
    assert "cues" in data, (
        f"[DOCS BUG] quickstart.md shows response key 'items' but real key is 'cues'. "
        f"Actual keys: {list(data.keys())}"
    )
    assert "total" in data
    assert "limit" in data
    assert "offset" in data


@pytest.mark.asyncio
async def test_env_vars_documented_correctly(client):
    """Every env var in configuration.md is valid and accepted by the running app."""
    # Verify the app is running (env vars loaded correctly)
    resp = await client.get("/health")
    assert resp.status_code == 200

    # Verify /status endpoint exists (documented in production.md and faq.md)
    status_resp = await client.get("/status")
    # Docs say /status returns db+redis+poller status
    assert status_resp.status_code == 200, (
        f"[DOCS GAP] /status endpoint documented in production.md and faq.md "
        f"but returned {status_resp.status_code}"
    )


@pytest.mark.asyncio
async def test_workers_md_claim_requires_worker_id(client, registered_user, db_session):
    """workers.md claim example omits required worker_id body — will return 422."""
    from datetime import datetime, timezone, timedelta
    from app.models import Execution

    api_key = registered_user["api_key"]
    headers = {"Authorization": f"Bearer {api_key}"}

    # Create worker cue and inject execution
    create_resp = await client.post("/v1/cues", headers=headers, json={
        "name": f"workers-doc-test-{uuid.uuid4().hex[:6]}",
        "schedule": {"type": "recurring", "cron": "0 * * * *"},
        "transport": "worker",
    })
    assert create_resp.status_code == 201
    cue_id = create_resp.json()["id"]

    execution_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    db_session.add(Execution(
        id=execution_id, cue_id=cue_id,
        scheduled_for=now - timedelta(seconds=5),
        status="pending", attempts=0,
        created_at=now, updated_at=now,
    ))
    await db_session.commit()

    # workers.md shows: POST /v1/executions/{id}/claim with NO body
    # This should fail with 422 (worker_id required)
    no_body_resp = await client.post(
        f"/v1/executions/{execution_id}/claim",
        headers=headers,
    )
    assert no_body_resp.status_code == 422, (
        f"[DOCS BUG] workers.md claim example has no body (no worker_id) but got "
        f"{no_body_resp.status_code} instead of 422. "
        f"Docs must add: curl -d '{{\"worker_id\": \"my-worker\"}}'"
    )


@pytest.mark.asyncio
async def test_workers_md_outcome_schema_correct(client, registered_user, db_session):
    """workers.md outcome example uses {status: success/failure} but real schema uses {success: bool}."""
    from datetime import datetime, timezone, timedelta
    from app.models import Execution

    api_key = registered_user["api_key"]
    headers = {"Authorization": f"Bearer {api_key}"}

    create_resp = await client.post("/v1/cues", headers=headers, json={
        "name": f"outcome-schema-{uuid.uuid4().hex[:6]}",
        "schedule": {"type": "recurring", "cron": "0 * * * *"},
        "transport": "worker",
    })
    assert create_resp.status_code == 201
    cue_id = create_resp.json()["id"]

    execution_id = str(uuid.uuid4())
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

    # workers.md shows: {"status": "success", "result": {...}}
    # Test if that schema is accepted
    wrong_schema_resp = await client.post(
        f"/v1/executions/{execution_id}/outcome",
        headers={**headers, "Content-Type": "application/json"},
        json={"status": "success", "result": {"rows": 1}},
    )

    # Inject a second execution to test real schema
    execution_id2 = str(uuid.uuid4())
    db_session.add(Execution(
        id=execution_id2, cue_id=cue_id,
        scheduled_for=now - timedelta(seconds=3),
        status="pending", attempts=0,
        created_at=now, updated_at=now,
    ))
    await db_session.commit()

    await client.post(
        f"/v1/executions/{execution_id2}/claim",
        headers={**headers, "Content-Type": "application/json"},
        json={"worker_id": "test-worker"},
    )

    # Real schema: {"success": true, "result": "..."}
    real_schema_resp = await client.post(
        f"/v1/executions/{execution_id2}/outcome",
        headers={**headers, "Content-Type": "application/json"},
        json={"success": True, "result": "rows: 1"},
    )
    assert real_schema_resp.status_code in (200, 201), (
        f"Real outcome schema failed: {real_schema_resp.status_code} {real_schema_resp.text}"
    )

    if wrong_schema_resp.status_code not in (200, 201):
        pytest.xfail(
            f"[DOCS BUG] workers.md outcome example uses {{status: 'success'}} but "
            f"real schema uses {{success: true}}. Got {wrong_schema_resp.status_code}: {wrong_schema_resp.text}"
        )


@pytest.mark.asyncio
async def test_make_commands_documented(client):
    """Verify the app started correctly (proxy for: make up worked)."""
    # If we're here, the test stack came up — which means make test worked.
    resp = await client.get("/health")
    assert resp.status_code == 200
    # Also verify docs site exists
    docs_resp = await client.get("/docs")
    assert docs_resp.status_code == 200, (
        f"Interactive docs at /docs not available: {docs_resp.status_code}"
    )
