"""Agent Directory productization (Phase A) — roster endpoint + last_seen_at hooks.

Ports cueapi/cueapi#630.

Tests pin:

1. Pure helpers: _build_roster_entry / _compute_roster_etag / _etag_matches /
   _format_relative / _derive_online_state / _bucketed_seen / _bump_last_seen_stmt.
2. ``GET /v1/agents/roster`` returns display-optimized snapshot.
3. ``last_seen_at`` is updated by ``POST /v1/messages`` (sender) and
   ``GET /v1/agents/{ref}/inbox`` (recipient).
4. Roster ``online`` derives correctly from last_seen_at age.
5. Caller-asserted status overrides activity-derived state.
6. Soft-deleted agents excluded.
7. ETag stable when unchanged; changes when roster mutates.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from app.models.agent import Agent
from app.routers.agents import _etag_matches
from app.services.agent_service import (
    _build_roster_entry,
    _bucketed_seen,
    _compute_roster_etag,
    _derive_online_state,
    _format_relative,
)
from app.services.inbox_service import _bump_last_seen_stmt


# ── Pure helper unit tests ───────────────────────────────────────────


def _fake_agent(**kw):
    defaults = {
        "slug": "test-agent",
        "display_name": "Test Agent",
        "last_seen_at": None,
        "status": "online",
        "webhook_url": None,
        "metadata_": {},
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def test_build_roster_entry_offline_no_activity():
    now = datetime.now(timezone.utc)
    entry, etag_part = _build_roster_entry(_fake_agent(last_seen_at=None), now)
    assert entry["online"] is False
    assert entry["status"] == "offline"
    assert entry["last_seen_relative"] == "never"
    assert entry["preferred_contact"] == "async"


def test_build_roster_entry_online_recent():
    now = datetime.now(timezone.utc)
    entry, _ = _build_roster_entry(
        _fake_agent(last_seen_at=now - timedelta(seconds=30)), now
    )
    assert entry["online"] is True
    assert entry["status"] == "online"


def test_build_roster_entry_with_webhook_is_sync():
    now = datetime.now(timezone.utc)
    entry, _ = _build_roster_entry(_fake_agent(webhook_url="https://x.com/wh"), now)
    assert entry["preferred_contact"] == "sync"


def test_build_roster_entry_metadata_description():
    now = datetime.now(timezone.utc)
    entry, _ = _build_roster_entry(
        _fake_agent(metadata_={"description": "does things"}), now
    )
    assert entry["description"] == "does things"


def test_build_roster_entry_caller_override():
    now = datetime.now(timezone.utc)
    entry, _ = _build_roster_entry(
        _fake_agent(last_seen_at=now, status="away"), now
    )
    assert entry["status"] == "away"
    assert entry["online"] is False


def test_compute_roster_etag_stable():
    parts = ["a|A||1|async|online|abc"]
    assert _compute_roster_etag(parts) == _compute_roster_etag(parts)


def test_compute_roster_etag_changes():
    a = _compute_roster_etag(["a|A||1|async|online|abc"])
    b = _compute_roster_etag(["a|A||0|async|offline|abc"])
    assert a != b


def test_etag_matches_exact():
    assert _etag_matches('W/"abc"', 'W/"abc"') is True


def test_etag_matches_whitespace():
    assert _etag_matches('  W/"abc"  ', 'W/"abc"') is True


def test_etag_matches_empty():
    assert _etag_matches(None, 'W/"abc"') is False
    assert _etag_matches("", 'W/"abc"') is False


def test_format_relative_buckets():
    now = datetime.now(timezone.utc)
    assert _format_relative(now, None) == "never"
    assert _format_relative(now, now - timedelta(seconds=30)) == "active now"
    assert _format_relative(now, now - timedelta(minutes=15)) == "15m ago"
    assert _format_relative(now, now - timedelta(hours=3)) == "3h ago"


def test_derive_online_state():
    now = datetime.now(timezone.utc)
    assert _derive_online_state(now, now - timedelta(seconds=60), "online")[0] is True
    assert _derive_online_state(now, now - timedelta(minutes=15), "online")[1] == "away"
    assert _derive_online_state(now, now - timedelta(hours=2), "online")[1] == "offline"
    assert _derive_online_state(now, None, "online")[1] == "offline"
    assert _derive_online_state(now, now, "offline")[1] == "offline"


def test_bucketed_seen_floors_to_5min():
    ts = datetime(2026, 5, 5, 17, 7, 32, tzinfo=timezone.utc)
    expected = int(datetime(2026, 5, 5, 17, 5, 0, tzinfo=timezone.utc).timestamp())
    assert _bucketed_seen(ts) == str(expected)


def test_bucketed_seen_none():
    assert _bucketed_seen(None) == ""


def test_bump_last_seen_stmt():
    stmt = _bump_last_seen_stmt("agt_x", datetime.now(timezone.utc))
    sql = str(stmt.compile(compile_kwargs={"literal_binds": False}))
    assert "agents" in sql.lower()
    assert "last_seen_at" in sql.lower()


# ── Integration tests ────────────────────────────────────────────────


async def _create_agent(client, auth_headers, slug, **extra):
    body = {"slug": slug, "display_name": slug.title()}
    body.update(extra)
    resp = await client.post("/v1/agents", json=body, headers=auth_headers)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_roster_endpoint_shape(client, auth_headers):
    await _create_agent(client, auth_headers, "rs-a", metadata={"description": "First"})
    await _create_agent(client, auth_headers, "rs-b")
    resp = await client.get("/v1/agents/roster", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "generated_at" in body
    names = {a["name"] for a in body["agents"]}
    assert {"rs-a", "rs-b"}.issubset(names)
    for entry in body["agents"]:
        assert "id" not in entry
        assert "webhook_secret" not in entry
        assert "online" in entry
        assert "preferred_contact" in entry


@pytest.mark.asyncio
async def test_last_seen_at_bumps_on_message_send(client, auth_headers, db_session):
    sender = await _create_agent(client, auth_headers, "ls-s")
    rcpt = await _create_agent(client, auth_headers, "ls-r")
    pre = (await db_session.execute(select(Agent).where(Agent.id == sender["id"]))).scalar_one()
    assert pre.last_seen_at is None
    resp = await client.post(
        "/v1/messages",
        json={"to": rcpt["id"], "body": "hi"},
        headers={**auth_headers, "X-Cueapi-From-Agent": sender["id"]},
    )
    assert resp.status_code == 201
    db_session.expire_all()
    post = (await db_session.execute(select(Agent).where(Agent.id == sender["id"]))).scalar_one()
    assert post.last_seen_at is not None


@pytest.mark.asyncio
async def test_last_seen_at_bumps_on_inbox_poll(client, auth_headers, db_session):
    sender = await _create_agent(client, auth_headers, "lp-s")
    rcpt = await _create_agent(client, auth_headers, "lp-r")
    await client.post(
        "/v1/messages",
        json={"to": rcpt["id"], "body": "x"},
        headers={**auth_headers, "X-Cueapi-From-Agent": sender["id"]},
    )
    resp = await client.get(f"/v1/agents/{rcpt['id']}/inbox", headers=auth_headers)
    assert resp.status_code == 200
    db_session.expire_all()
    post = (await db_session.execute(select(Agent).where(Agent.id == rcpt["id"]))).scalar_one()
    assert post.last_seen_at is not None


@pytest.mark.asyncio
async def test_roster_excludes_soft_deleted(client, auth_headers):
    await _create_agent(client, auth_headers, "rk")
    drop = await _create_agent(client, auth_headers, "rd")
    del_resp = await client.delete(f"/v1/agents/{drop['id']}", headers=auth_headers)
    assert del_resp.status_code == 204
    resp = await client.get("/v1/agents/roster", headers=auth_headers)
    names = {a["name"] for a in resp.json()["agents"]}
    assert "rk" in names
    assert "rd" not in names


@pytest.mark.asyncio
async def test_roster_etag_304_when_unchanged(client, auth_headers):
    await _create_agent(client, auth_headers, "et-1")
    first = await client.get("/v1/agents/roster", headers=auth_headers)
    etag = first.headers.get("etag")
    assert etag is not None
    second = await client.get(
        "/v1/agents/roster", headers={**auth_headers, "If-None-Match": etag}
    )
    assert second.status_code == 304


@pytest.mark.asyncio
async def test_roster_etag_changes_when_agent_added(client, auth_headers):
    await _create_agent(client, auth_headers, "ec-1")
    first = await client.get("/v1/agents/roster", headers=auth_headers)
    etag1 = first.headers.get("etag")
    await _create_agent(client, auth_headers, "ec-2")
    second = await client.get("/v1/agents/roster", headers=auth_headers)
    etag2 = second.headers.get("etag")
    assert etag1 != etag2
