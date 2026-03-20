"""Cue CRUD edge case tests — pagination, filtering, name limits, cron edge cases.

Fills gaps in test_cues.py for boundary conditions and query parameter coverage.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


# ---- Pagination tests ----

@pytest.mark.asyncio
async def test_list_cues_with_limit(client, auth_headers):
    """GET /v1/cues?limit=2 should return at most 2 cues."""
    for i in range(5):
        await client.post("/v1/cues", headers=auth_headers, json={
            "name": f"page-cue-{i}",
            "schedule": {"type": "recurring", "cron": "0 9 * * *"},
            "callback": {"url": "https://example.com/webhook"}
        })

    resp = await client.get("/v1/cues?limit=2", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["cues"]) == 2
    assert data["total"] == 5
    assert data["limit"] == 2
    assert data["offset"] == 0


@pytest.mark.asyncio
async def test_list_cues_with_offset(client, auth_headers):
    """GET /v1/cues?offset=3 should skip the first 3 cues."""
    for i in range(5):
        await client.post("/v1/cues", headers=auth_headers, json={
            "name": f"offset-cue-{i}",
            "schedule": {"type": "recurring", "cron": "0 9 * * *"},
            "callback": {"url": "https://example.com/webhook"}
        })

    resp = await client.get("/v1/cues?offset=3", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["cues"]) == 2  # 5 total, skip 3 = 2 remaining
    assert data["total"] == 5
    assert data["offset"] == 3


@pytest.mark.asyncio
async def test_list_cues_with_limit_and_offset(client, auth_headers):
    """GET /v1/cues?limit=2&offset=1 should paginate correctly."""
    for i in range(5):
        await client.post("/v1/cues", headers=auth_headers, json={
            "name": f"combo-cue-{i}",
            "schedule": {"type": "recurring", "cron": "0 9 * * *"},
            "callback": {"url": "https://example.com/webhook"}
        })

    resp = await client.get("/v1/cues?limit=2&offset=1", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["cues"]) == 2
    assert data["total"] == 5
    assert data["limit"] == 2
    assert data["offset"] == 1


@pytest.mark.asyncio
async def test_list_cues_offset_beyond_total(client, auth_headers):
    """Offset beyond total cues should return empty list."""
    await client.post("/v1/cues", headers=auth_headers, json={
        "name": "only-one",
        "schedule": {"type": "recurring", "cron": "0 9 * * *"},
        "callback": {"url": "https://example.com/webhook"}
    })

    resp = await client.get("/v1/cues?offset=100", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["cues"]) == 0
    assert data["total"] == 1  # Total is still correct


@pytest.mark.asyncio
async def test_list_cues_invalid_limit_zero(client, auth_headers):
    """limit=0 should return 422 (ge=1 constraint)."""
    resp = await client.get("/v1/cues?limit=0", headers=auth_headers)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_cues_limit_over_max(client, auth_headers):
    """limit=200 should return 422 (le=100 constraint)."""
    resp = await client.get("/v1/cues?limit=200", headers=auth_headers)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_cues_negative_offset(client, auth_headers):
    """Negative offset should return 422 (ge=0 constraint)."""
    resp = await client.get("/v1/cues?offset=-1", headers=auth_headers)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_cues_ordered_by_created_at_desc(client, auth_headers):
    """Cues should be returned newest first (created_at DESC)."""
    for i in range(3):
        await client.post("/v1/cues", headers=auth_headers, json={
            "name": f"order-cue-{i}",
            "schedule": {"type": "recurring", "cron": "0 9 * * *"},
            "callback": {"url": "https://example.com/webhook"}
        })

    resp = await client.get("/v1/cues", headers=auth_headers)
    cues = resp.json()["cues"]
    assert len(cues) == 3
    # Newest first
    assert cues[0]["name"] == "order-cue-2"
    assert cues[2]["name"] == "order-cue-0"


# ---- Filter tests ----

@pytest.mark.asyncio
async def test_list_cues_filter_with_pagination(client, auth_headers):
    """Status filter + pagination should work together."""
    for i in range(4):
        resp = await client.post("/v1/cues", headers=auth_headers, json={
            "name": f"fp-cue-{i}",
            "schedule": {"type": "recurring", "cron": "0 9 * * *"},
            "callback": {"url": "https://example.com/webhook"}
        })
        if i >= 2:
            cue_id = resp.json()["id"]
            await client.patch(f"/v1/cues/{cue_id}", headers=auth_headers, json={"status": "paused"})

    resp = await client.get("/v1/cues?status=active&limit=1", headers=auth_headers)
    data = resp.json()
    assert len(data["cues"]) == 1
    assert data["total"] == 2  # 2 active, with limit=1


# ---- Name edge cases ----

@pytest.mark.asyncio
async def test_create_cue_duplicate_names_rejected(client, auth_headers):
    """Same name for two cues by the same user should be rejected with 409."""
    resp = await client.post("/v1/cues", headers=auth_headers, json={
        "name": "duplicate-name",
        "schedule": {"type": "recurring", "cron": "0 9 * * *"},
        "callback": {"url": "https://example.com/webhook"}
    })
    assert resp.status_code == 201
    resp = await client.post("/v1/cues", headers=auth_headers, json={
        "name": "duplicate-name",
        "schedule": {"type": "recurring", "cron": "0 9 * * *"},
        "callback": {"url": "https://example.com/webhook"}
    })
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "duplicate_cue_name"


@pytest.mark.asyncio
async def test_create_cue_max_name_length(client, auth_headers):
    """Name at exactly 255 characters should be accepted."""
    resp = await client.post("/v1/cues", headers=auth_headers, json={
        "name": "x" * 255,
        "schedule": {"type": "recurring", "cron": "0 9 * * *"},
        "callback": {"url": "https://example.com/webhook"}
    })
    assert resp.status_code == 201
    assert len(resp.json()["name"]) == 255


@pytest.mark.asyncio
async def test_create_cue_name_too_long(client, auth_headers):
    """Name over 255 characters should return 422."""
    resp = await client.post("/v1/cues", headers=auth_headers, json={
        "name": "x" * 256,
        "schedule": {"type": "recurring", "cron": "0 9 * * *"},
        "callback": {"url": "https://example.com/webhook"}
    })
    assert resp.status_code == 422


# ---- Cron edge cases ----

@pytest.mark.asyncio
async def test_create_cue_cron_every_minute(client, auth_headers):
    """'* * * * *' (every minute) is valid cron."""
    resp = await client.post("/v1/cues", headers=auth_headers, json={
        "name": "every-minute",
        "schedule": {"type": "recurring", "cron": "* * * * *"},
        "callback": {"url": "https://example.com/webhook"}
    })
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_create_cue_cron_empty_string(client, auth_headers):
    """Empty string cron should return 400."""
    resp = await client.post("/v1/cues", headers=auth_headers, json={
        "name": "empty-cron",
        "schedule": {"type": "recurring", "cron": ""},
        "callback": {"url": "https://example.com/webhook"}
    })
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_schedule"


@pytest.mark.asyncio
async def test_create_cue_recurring_without_cron(client, auth_headers):
    """Recurring schedule without cron field should return 400."""
    resp = await client.post("/v1/cues", headers=auth_headers, json={
        "name": "no-cron",
        "schedule": {"type": "recurring"},
        "callback": {"url": "https://example.com/webhook"}
    })
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_schedule"


@pytest.mark.asyncio
async def test_create_cue_once_without_at(client, auth_headers):
    """Once schedule without 'at' field should return 400."""
    resp = await client.post("/v1/cues", headers=auth_headers, json={
        "name": "no-at",
        "schedule": {"type": "once"},
        "callback": {"url": "https://example.com/webhook"}
    })
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_schedule"


@pytest.mark.asyncio
async def test_create_cue_invalid_schedule_type(client, auth_headers):
    """Invalid schedule type should return 400."""
    resp = await client.post("/v1/cues", headers=auth_headers, json={
        "name": "bad-type",
        "schedule": {"type": "interval", "cron": "0 9 * * *"},
        "callback": {"url": "https://example.com/webhook"}
    })
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_schedule"


# ---- Payload edge cases ----

@pytest.mark.asyncio
async def test_create_cue_empty_payload(client, auth_headers):
    """Empty payload {} should be accepted."""
    resp = await client.post("/v1/cues", headers=auth_headers, json={
        "name": "empty-payload",
        "schedule": {"type": "recurring", "cron": "0 9 * * *"},
        "callback": {"url": "https://example.com/webhook"},
        "payload": {}
    })
    assert resp.status_code == 201
    assert resp.json()["payload"] == {}


@pytest.mark.asyncio
async def test_create_cue_no_payload(client, auth_headers):
    """Missing payload field should default to {}."""
    resp = await client.post("/v1/cues", headers=auth_headers, json={
        "name": "no-payload",
        "schedule": {"type": "recurring", "cron": "0 9 * * *"},
        "callback": {"url": "https://example.com/webhook"}
    })
    assert resp.status_code == 201
    assert resp.json()["payload"] == {}


# ---- Description tests ----

@pytest.mark.asyncio
async def test_create_cue_with_description(client, auth_headers):
    """Cue with description should store and return it."""
    resp = await client.post("/v1/cues", headers=auth_headers, json={
        "name": "described",
        "description": "This is a test cue for analytics checking",
        "schedule": {"type": "recurring", "cron": "0 9 * * *"},
        "callback": {"url": "https://example.com/webhook"}
    })
    assert resp.status_code == 201
    assert resp.json()["description"] == "This is a test cue for analytics checking"


@pytest.mark.asyncio
async def test_patch_update_name(client, auth_headers):
    """PATCH with name should update the cue name."""
    resp = await client.post("/v1/cues", headers=auth_headers, json={
        "name": "old-name",
        "schedule": {"type": "recurring", "cron": "0 9 * * *"},
        "callback": {"url": "https://example.com/webhook"}
    })
    cue_id = resp.json()["id"]

    resp = await client.patch(f"/v1/cues/{cue_id}", headers=auth_headers, json={
        "name": "new-name"
    })
    assert resp.status_code == 200
    assert resp.json()["name"] == "new-name"


@pytest.mark.asyncio
async def test_patch_update_description(client, auth_headers):
    """PATCH with description should update it."""
    resp = await client.post("/v1/cues", headers=auth_headers, json={
        "name": "desc-update",
        "schedule": {"type": "recurring", "cron": "0 9 * * *"},
        "callback": {"url": "https://example.com/webhook"}
    })
    cue_id = resp.json()["id"]

    resp = await client.patch(f"/v1/cues/{cue_id}", headers=auth_headers, json={
        "description": "Updated description"
    })
    assert resp.status_code == 200
    assert resp.json()["description"] == "Updated description"


@pytest.mark.asyncio
async def test_patch_nonexistent_cue(client, auth_headers):
    """PATCH on a cue that doesn't exist should return 404."""
    resp = await client.patch("/v1/cues/cue_doesnotexist", headers=auth_headers, json={
        "name": "ghost"
    })
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_cue_response_has_all_fields(client, auth_headers):
    """Created cue response should have all expected fields."""
    resp = await client.post("/v1/cues", headers=auth_headers, json={
        "name": "full-response",
        "schedule": {"type": "recurring", "cron": "0 9 * * *"},
        "callback": {"url": "https://example.com/webhook"},
        "payload": {"task": "check"}
    })
    assert resp.status_code == 201
    data = resp.json()

    # Verify all required fields exist
    required_fields = [
        "id", "name", "status", "transport", "schedule", "callback",
        "payload", "retry", "next_run", "last_run", "run_count",
        "created_at", "updated_at"
    ]
    for field in required_fields:
        assert field in data, f"Missing field: {field}"

    assert data["id"].startswith("cue_")
    assert data["status"] == "active"
    assert data["transport"] == "webhook"
    assert data["run_count"] == 0
    assert data["last_run"] is None
