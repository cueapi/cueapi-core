from datetime import datetime, timedelta, timezone

import pytest


@pytest.mark.asyncio
async def test_create_recurring_cue(client, auth_headers):
    response = await client.post("/v1/cues", headers=auth_headers, json={
        "name": "morning-check",
        "schedule": {"type": "recurring", "cron": "0 9 * * *", "timezone": "UTC"},
        "callback": {"url": "https://example.com/webhook"},
        "payload": {"task": "check_analytics"}
    })
    assert response.status_code == 201
    data = response.json()
    assert data["id"].startswith("cue_")
    assert data["status"] == "active"
    assert data["next_run"] is not None


@pytest.mark.asyncio
async def test_create_onetime_cue(client, auth_headers):
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    response = await client.post("/v1/cues", headers=auth_headers, json={
        "name": "one-shot",
        "schedule": {"type": "once", "at": future},
        "callback": {"url": "https://example.com/webhook"}
    })
    assert response.status_code == 201


@pytest.mark.asyncio
async def test_create_cue_invalid_cron(client, auth_headers):
    response = await client.post("/v1/cues", headers=auth_headers, json={
        "name": "bad-cron",
        "schedule": {"type": "recurring", "cron": "not a cron"},
        "callback": {"url": "https://example.com/webhook"}
    })
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_schedule"


@pytest.mark.asyncio
async def test_create_cue_past_timestamp(client, auth_headers):
    response = await client.post("/v1/cues", headers=auth_headers, json={
        "name": "past-cue",
        "schedule": {"type": "once", "at": "2020-01-01T00:00:00Z"},
        "callback": {"url": "https://example.com/webhook"}
    })
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_schedule"


@pytest.mark.asyncio
async def test_create_cue_exceeds_limit(client, auth_headers):
    for i in range(10):
        resp = await client.post("/v1/cues", headers=auth_headers, json={
            "name": f"cue-{i}",
            "schedule": {"type": "recurring", "cron": "0 9 * * *"},
            "callback": {"url": "https://example.com/webhook"}
        })
        assert resp.status_code == 201

    response = await client.post("/v1/cues", headers=auth_headers, json={
        "name": "cue-overflow",
        "schedule": {"type": "recurring", "cron": "0 9 * * *"},
        "callback": {"url": "https://example.com/webhook"}
    })
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "cue_limit_exceeded"


@pytest.mark.asyncio
async def test_list_cues_empty(client, auth_headers):
    response = await client.get("/v1/cues", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["cues"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_list_cues_returns_only_own(client, auth_headers, other_auth_headers):
    await client.post("/v1/cues", headers=auth_headers, json={
        "name": "user-a-cue",
        "schedule": {"type": "recurring", "cron": "0 9 * * *"},
        "callback": {"url": "https://example.com/webhook"}
    })
    response = await client.get("/v1/cues", headers=other_auth_headers)
    assert response.json()["total"] == 0


@pytest.mark.asyncio
async def test_list_cues_status_filter(client, auth_headers):
    resp = await client.post("/v1/cues", headers=auth_headers, json={
        "name": "filter-test",
        "schedule": {"type": "recurring", "cron": "0 9 * * *"},
        "callback": {"url": "https://example.com/webhook"}
    })
    cue_id = resp.json()["id"]
    await client.patch(f"/v1/cues/{cue_id}", headers=auth_headers, json={"status": "paused"})
    response = await client.get("/v1/cues?status=active", headers=auth_headers)
    assert response.json()["total"] == 0
    response = await client.get("/v1/cues?status=paused", headers=auth_headers)
    assert response.json()["total"] == 1


@pytest.mark.asyncio
async def test_get_cue(client, auth_headers):
    resp = await client.post("/v1/cues", headers=auth_headers, json={
        "name": "get-me",
        "schedule": {"type": "recurring", "cron": "0 9 * * *"},
        "callback": {"url": "https://example.com/webhook"}
    })
    cue_id = resp.json()["id"]
    response = await client.get(f"/v1/cues/{cue_id}", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["name"] == "get-me"


@pytest.mark.asyncio
async def test_get_cue_not_found(client, auth_headers):
    response = await client.get("/v1/cues/cue_doesnotexist", headers=auth_headers)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_other_users_cue_returns_404(client, auth_headers, other_auth_headers):
    resp = await client.post("/v1/cues", headers=auth_headers, json={
        "name": "private",
        "schedule": {"type": "recurring", "cron": "0 9 * * *"},
        "callback": {"url": "https://example.com/webhook"}
    })
    cue_id = resp.json()["id"]
    response = await client.get(f"/v1/cues/{cue_id}", headers=other_auth_headers)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_pause_cue(client, auth_headers):
    resp = await client.post("/v1/cues", headers=auth_headers, json={
        "name": "pause-me",
        "schedule": {"type": "recurring", "cron": "0 9 * * *"},
        "callback": {"url": "https://example.com/webhook"}
    })
    cue_id = resp.json()["id"]
    response = await client.patch(f"/v1/cues/{cue_id}", headers=auth_headers, json={"status": "paused"})
    assert response.status_code == 200
    assert response.json()["status"] == "paused"
    assert response.json()["next_run"] is None


@pytest.mark.asyncio
async def test_resume_cue(client, auth_headers):
    resp = await client.post("/v1/cues", headers=auth_headers, json={
        "name": "resume-me",
        "schedule": {"type": "recurring", "cron": "0 9 * * *"},
        "callback": {"url": "https://example.com/webhook"}
    })
    cue_id = resp.json()["id"]
    await client.patch(f"/v1/cues/{cue_id}", headers=auth_headers, json={"status": "paused"})
    response = await client.patch(f"/v1/cues/{cue_id}", headers=auth_headers, json={"status": "active"})
    assert response.status_code == 200
    assert response.json()["status"] == "active"
    assert response.json()["next_run"] is not None


@pytest.mark.asyncio
async def test_update_schedule(client, auth_headers):
    resp = await client.post("/v1/cues", headers=auth_headers, json={
        "name": "reschedule",
        "schedule": {"type": "recurring", "cron": "0 9 * * *"},
        "callback": {"url": "https://example.com/webhook"}
    })
    cue_id = resp.json()["id"]
    old_next_run = resp.json()["next_run"]
    response = await client.patch(f"/v1/cues/{cue_id}", headers=auth_headers, json={
        "schedule": {"type": "recurring", "cron": "0 10 * * *", "timezone": "UTC"}
    })
    assert response.status_code == 200
    assert response.json()["next_run"] != old_next_run


@pytest.mark.asyncio
async def test_delete_cue(client, auth_headers):
    resp = await client.post("/v1/cues", headers=auth_headers, json={
        "name": "delete-me",
        "schedule": {"type": "recurring", "cron": "0 9 * * *"},
        "callback": {"url": "https://example.com/webhook"}
    })
    cue_id = resp.json()["id"]
    response = await client.delete(f"/v1/cues/{cue_id}", headers=auth_headers)
    assert response.status_code == 204
    response = await client.get(f"/v1/cues/{cue_id}", headers=auth_headers)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_delete_other_users_cue_returns_404(client, auth_headers, other_auth_headers):
    resp = await client.post("/v1/cues", headers=auth_headers, json={
        "name": "not-yours",
        "schedule": {"type": "recurring", "cron": "0 9 * * *"},
        "callback": {"url": "https://example.com/webhook"}
    })
    cue_id = resp.json()["id"]
    response = await client.delete(f"/v1/cues/{cue_id}", headers=other_auth_headers)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_patch_rejects_unknown_fields(client, auth_headers):
    """PATCH with unknown fields like 'transport' should return 422, not silently ignore."""
    resp = await client.post("/v1/cues", headers=auth_headers, json={
        "name": "patch-unknown",
        "schedule": {"type": "recurring", "cron": "0 9 * * *"},
        "callback": {"url": "https://example.com/webhook"}
    })
    cue_id = resp.json()["id"]
    response = await client.patch(f"/v1/cues/{cue_id}", headers=auth_headers, json={
        "transport": "worker"
    })
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_patch_rejects_invalid_status(client, auth_headers):
    """PATCH with invalid status like 'disabled' should return 422."""
    resp = await client.post("/v1/cues", headers=auth_headers, json={
        "name": "patch-invalid-status",
        "schedule": {"type": "recurring", "cron": "0 9 * * *"},
        "callback": {"url": "https://example.com/webhook"}
    })
    cue_id = resp.json()["id"]
    response = await client.patch(f"/v1/cues/{cue_id}", headers=auth_headers, json={
        "status": "disabled"
    })
    assert response.status_code == 422
