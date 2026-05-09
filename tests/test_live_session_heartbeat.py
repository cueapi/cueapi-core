"""POST /v1/agents/{ref}/live-sessions/{label}/heartbeat tests.

Bumps `last_heartbeat` on the active session with this label.
Optionally accepts `monitor_version` in the request body for
in-place version refresh.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest


async def _make_agent(client, headers, slug=None):
    payload = {"display_name": f"Agent {uuid.uuid4().hex[:6]}", "metadata": {}}
    if slug:
        payload["slug"] = slug
    r = await client.post("/v1/agents", json=payload, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _register_body(label="main", **kwargs):
    body = {
        "label": label,
        "cue_id": f"cue_{uuid.uuid4().hex[:12]}",
        "task_name": f"task-{uuid.uuid4().hex[:8]}-live",
    }
    body.update(kwargs)
    return body


@pytest.mark.asyncio
async def test_heartbeat_bumps_last_heartbeat(client, auth_headers):
    agent = await _make_agent(client, auth_headers)
    r1 = await client.post(
        f"/v1/agents/{agent['id']}/live-sessions",
        json=_register_body(),
        headers=auth_headers,
    )
    assert r1.status_code == 201
    assert r1.json()["heartbeat_age_sec"] is not None  # initial heartbeat exists

    # Sleep a moment so the bumped heartbeat has a measurable delta.
    await asyncio.sleep(1.1)

    r2 = await client.post(
        f"/v1/agents/{agent['id']}/live-sessions/main/heartbeat",
        headers=auth_headers,
    )
    assert r2.status_code == 200
    bumped_age = r2.json()["heartbeat_age_sec"]
    # After bump, heartbeat_age should be ~0 (we just set it to now).
    assert bumped_age is not None and bumped_age <= 1


@pytest.mark.asyncio
async def test_heartbeat_with_monitor_version_refresh(client, auth_headers):
    agent = await _make_agent(client, auth_headers)
    await client.post(
        f"/v1/agents/{agent['id']}/live-sessions",
        json=_register_body(monitor_version="v1.0.0"),
        headers=auth_headers,
    )
    r = await client.post(
        f"/v1/agents/{agent['id']}/live-sessions/main/heartbeat",
        json={"monitor_version": "v2.1.0"},
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert r.json()["monitor_version"] == "v2.1.0"


@pytest.mark.asyncio
async def test_heartbeat_unknown_label_404(client, auth_headers):
    agent = await _make_agent(client, auth_headers)
    r = await client.post(
        f"/v1/agents/{agent['id']}/live-sessions/nonexistent/heartbeat",
        headers=auth_headers,
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "live_session_not_found"


@pytest.mark.asyncio
async def test_heartbeat_after_detach_404(client, auth_headers):
    """A detached session (detached_at IS NOT NULL) should not respond
    to heartbeat — the partial unique index excludes it from the
    `WHERE detached_at IS NULL` predicate."""
    agent = await _make_agent(client, auth_headers)
    await client.post(
        f"/v1/agents/{agent['id']}/live-sessions",
        json=_register_body(),
        headers=auth_headers,
    )
    await client.delete(
        f"/v1/agents/{agent['id']}/live-sessions/main",
        headers=auth_headers,
    )
    r = await client.post(
        f"/v1/agents/{agent['id']}/live-sessions/main/heartbeat",
        headers=auth_headers,
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_heartbeat_cross_user_404(client, auth_headers, other_auth_headers):
    agent = await _make_agent(client, other_auth_headers)
    await client.post(
        f"/v1/agents/{agent['id']}/live-sessions",
        json=_register_body(),
        headers=other_auth_headers,
    )
    # User A heartbeats user B's session — should 404.
    r = await client.post(
        f"/v1/agents/{agent['id']}/live-sessions/main/heartbeat",
        headers=auth_headers,
    )
    assert r.status_code == 404
