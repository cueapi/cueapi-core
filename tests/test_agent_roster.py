"""Agent Directory productization (Phase A) — roster endpoint + last_seen_at hooks.

Spec: PRD https://trydock.ai/mike/agent-directory-productization-prd §Server-side scope.

These tests pin:

1. ``GET /v1/agents/roster`` returns display-optimized snapshot (no
   IDs, secrets, timestamps); always-full list, no pagination.
2. ``last_seen_at`` is updated by ``POST /v1/messages`` for the sender's agent.
3. ``last_seen_at`` is updated by ``GET /v1/agents/{ref}/inbox`` for the recipient's agent.
4. Roster's ``online`` field derives correctly from last_seen_at age.
5. Caller-asserted ``status=away|offline`` overrides the activity-derived state.
6. Soft-deleted agents are excluded from the roster.
7. Roster ``preferred_contact`` derives from webhook_url presence.
8. ``last_seen_relative`` formats ages correctly.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from app.models.agent import Agent
from app.services.agent_service import (
    _build_roster_entry,
    _bucketed_seen,
    _compute_roster_etag,
    _derive_online_state,
    _format_relative,
)
from app.services.inbox_service import _bump_last_seen_stmt
from app.routers.agents import _etag_matches


def test_etag_matches_exact():
    assert _etag_matches('W/"abc123"', 'W/"abc123"') is True


def test_etag_matches_with_whitespace():
    """Some clients send leading/trailing whitespace in the header."""
    assert _etag_matches('  W/"abc123"  ', 'W/"abc123"') is True


def test_etag_matches_mismatch():
    assert _etag_matches('W/"old"', 'W/"new"') is False


def test_etag_matches_empty_header():
    assert _etag_matches(None, 'W/"abc"') is False
    assert _etag_matches("", 'W/"abc"') is False


# ── Pure helper unit tests (Phase A branch coverage) ─────────────────


def _fake_agent(**kw):
    """Duck-typed Agent for testing _build_roster_entry without a DB row."""
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
    a = _fake_agent(last_seen_at=None)
    entry, etag_part = _build_roster_entry(a, now)
    assert entry["online"] is False
    assert entry["status"] == "offline"
    assert entry["last_seen_relative"] == "never"
    assert entry["preferred_contact"] == "async"
    assert entry["description"] is None
    assert "test-agent|Test Agent||0|async|offline|" == etag_part


def test_build_roster_entry_online_recent_activity():
    now = datetime.now(timezone.utc)
    a = _fake_agent(last_seen_at=now - timedelta(seconds=30))
    entry, _ = _build_roster_entry(a, now)
    assert entry["online"] is True
    assert entry["status"] == "online"
    assert entry["last_seen_relative"] == "active now"


def test_build_roster_entry_with_webhook_is_sync():
    now = datetime.now(timezone.utc)
    a = _fake_agent(webhook_url="https://example.com/wh")
    entry, _ = _build_roster_entry(a, now)
    assert entry["preferred_contact"] == "sync"


def test_build_roster_entry_with_metadata_description():
    now = datetime.now(timezone.utc)
    a = _fake_agent(metadata_={"description": "an agent that does things"})
    entry, _ = _build_roster_entry(a, now)
    assert entry["description"] == "an agent that does things"


def test_build_roster_entry_caller_override_wins():
    """Caller-asserted away/offline beats activity-derived online."""
    now = datetime.now(timezone.utc)
    a = _fake_agent(last_seen_at=now, status="away")  # active but caller said away
    entry, _ = _build_roster_entry(a, now)
    assert entry["status"] == "away"
    assert entry["online"] is False


def test_compute_roster_etag_stable_for_same_input():
    parts = ["a|A||1|async|online|abc", "b|B||0|sync|offline|"]
    assert _compute_roster_etag(parts) == _compute_roster_etag(parts)
    # Format: weak ETag with W/" prefix and " suffix.
    e = _compute_roster_etag(parts)
    assert e.startswith('W/"') and e.endswith('"')


def test_compute_roster_etag_changes_when_input_changes():
    a = _compute_roster_etag(["a|A||1|async|online|abc"])
    b = _compute_roster_etag(["a|A||0|async|offline|abc"])
    assert a != b


def test_compute_roster_etag_empty_list():
    """No agents → still a valid etag (sha256 of empty string)."""
    e = _compute_roster_etag([])
    assert e.startswith('W/"')
    assert len(e) == len('W/"') + 16 + 1  # 16 hex chars + closing quote


def test_format_relative_buckets():
    now = datetime.now(timezone.utc)
    assert _format_relative(now, None) == "never"
    assert _format_relative(now, now - timedelta(seconds=30)) == "active now"
    assert _format_relative(now, now - timedelta(minutes=15)) == "15m ago"
    assert _format_relative(now, now - timedelta(hours=3)) == "3h ago"
    assert _format_relative(now, now - timedelta(days=2)) == "2d ago"


def test_derive_online_state_thresholds():
    now = datetime.now(timezone.utc)
    # Within 5 min → online.
    assert _derive_online_state(now, now - timedelta(seconds=60), "online")[0] is True
    # 5-30 min → away.
    assert _derive_online_state(now, now - timedelta(minutes=15), "online")[1] == "away"
    # >30 min → offline.
    assert _derive_online_state(now, now - timedelta(hours=2), "online")[1] == "offline"
    # NULL → offline.
    assert _derive_online_state(now, None, "online")[1] == "offline"
    # Caller override wins.
    assert _derive_online_state(now, now, "offline")[1] == "offline"


def test_bucketed_seen_floors_to_5min():
    ts = datetime(2026, 5, 5, 17, 7, 32, tzinfo=timezone.utc)  # 17:07:32
    bucketed = _bucketed_seen(ts)
    # 17:07:32 → 17:05:00 epoch.
    expected_epoch = int(datetime(2026, 5, 5, 17, 5, 0, tzinfo=timezone.utc).timestamp())
    assert bucketed == str(expected_epoch)


def test_bucketed_seen_none():
    assert _bucketed_seen(None) == ""


def test_bump_last_seen_stmt_constructs_update():
    """_bump_last_seen_stmt returns a SQLAlchemy UPDATE; sanity-check shape."""
    from datetime import datetime as dt, timezone as tz
    stmt = _bump_last_seen_stmt("agt_test123", dt.now(tz.utc))
    # The compiled SQL targets the agents table and sets last_seen_at.
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": False}))
    assert "agents" in compiled.lower()
    assert "last_seen_at" in compiled.lower()


# ── Integration tests (HTTP + DB end-to-end) ─────────────────────────





async def _create_agent(client, auth_headers, slug, **extra):
    body = {"slug": slug, "display_name": slug.title()}
    body.update(extra)
    resp = await client.post("/v1/agents", json=body, headers=auth_headers)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_roster_endpoint_returns_display_shape(client, auth_headers):
    await _create_agent(client, auth_headers, "roster-a", metadata={"description": "First agent"})
    await _create_agent(client, auth_headers, "roster-b")

    resp = await client.get("/v1/agents/roster", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "generated_at" in body
    assert "agents" in body
    names = {a["name"] for a in body["agents"]}
    assert {"roster-a", "roster-b"}.issubset(names)

    # Each entry has display-optimized fields (NO id, NO webhook_secret).
    for entry in body["agents"]:
        assert "name" in entry
        assert "display_name" in entry
        assert "online" in entry
        assert "last_seen_relative" in entry
        assert "preferred_contact" in entry
        assert "status" in entry
        # Display shape must NOT leak management surface fields.
        assert "id" not in entry
        assert "webhook_secret" not in entry
        assert "user_id" not in entry
        assert "created_at" not in entry
        assert "deleted_at" not in entry

    # The agent with metadata.description surfaces it.
    a_entry = next(a for a in body["agents"] if a["name"] == "roster-a")
    assert a_entry["description"] == "First agent"
    b_entry = next(a for a in body["agents"] if a["name"] == "roster-b")
    assert b_entry["description"] is None


@pytest.mark.asyncio
async def test_last_seen_at_bumps_on_message_send(client, auth_headers, db_session):
    sender = await _create_agent(client, auth_headers, "lsa-sender")
    rcpt = await _create_agent(client, auth_headers, "lsa-rcpt")

    # Pre-condition: sender's last_seen_at is NULL (never wrote a message).
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
    # Within last 5 seconds of "now".
    age = (datetime.now(timezone.utc) - post.last_seen_at).total_seconds()
    assert 0 <= age < 5


@pytest.mark.asyncio
async def test_last_seen_at_bumps_on_inbox_poll(client, auth_headers, db_session):
    # Two agents; sender writes a message so recipient has something to poll.
    sender = await _create_agent(client, auth_headers, "lsa-poll-s")
    rcpt = await _create_agent(client, auth_headers, "lsa-poll-r")
    await client.post(
        "/v1/messages",
        json={"to": rcpt["id"], "body": "x"},
        headers={**auth_headers, "X-Cueapi-From-Agent": sender["id"]},
    )

    pre = (await db_session.execute(select(Agent).where(Agent.id == rcpt["id"]))).scalar_one()
    pre_seen = pre.last_seen_at  # may be NULL

    resp = await client.get(f"/v1/agents/{rcpt['id']}/inbox", headers=auth_headers)
    assert resp.status_code == 200

    db_session.expire_all()
    post = (await db_session.execute(select(Agent).where(Agent.id == rcpt["id"]))).scalar_one()
    assert post.last_seen_at is not None
    if pre_seen is not None:
        assert post.last_seen_at >= pre_seen
    age = (datetime.now(timezone.utc) - post.last_seen_at).total_seconds()
    assert 0 <= age < 5


@pytest.mark.asyncio
async def test_roster_online_derivation(client, auth_headers, db_session):
    fresh = await _create_agent(client, auth_headers, "online-fresh")
    stale = await _create_agent(client, auth_headers, "online-stale")
    cold = await _create_agent(client, auth_headers, "online-cold")

    # Backdate the agents to specific recencies.
    now = datetime.now(timezone.utc)
    await db_session.execute(
        update(Agent).where(Agent.id == fresh["id"]).values(last_seen_at=now - timedelta(seconds=30))
    )
    await db_session.execute(
        update(Agent).where(Agent.id == stale["id"]).values(last_seen_at=now - timedelta(minutes=15))
    )
    await db_session.execute(
        update(Agent).where(Agent.id == cold["id"]).values(last_seen_at=now - timedelta(hours=2))
    )
    await db_session.commit()

    resp = await client.get("/v1/agents/roster", headers=auth_headers)
    assert resp.status_code == 200
    by_name = {a["name"]: a for a in resp.json()["agents"]}

    assert by_name["online-fresh"]["online"] is True
    assert by_name["online-fresh"]["status"] == "online"
    # 15 min ago → away (not online, not yet offline at 30 min boundary).
    assert by_name["online-stale"]["online"] is False
    assert by_name["online-stale"]["status"] == "away"
    # 2h ago → offline.
    assert by_name["online-cold"]["online"] is False
    assert by_name["online-cold"]["status"] == "offline"


@pytest.mark.asyncio
async def test_caller_asserted_status_overrides_derivation(client, auth_headers, db_session):
    """Agent voluntarily marked itself away — even with fresh activity,
    the override sticks so the agent can signal 'I'm here but busy'."""
    a = await _create_agent(client, auth_headers, "override-away")

    # Mark recent activity. Commit BEFORE issuing the PATCH so the
    # request handler's session doesn't deadlock on a row lock the
    # test's session is holding (both target the same agent row).
    now = datetime.now(timezone.utc)
    await db_session.execute(
        update(Agent).where(Agent.id == a["id"]).values(last_seen_at=now)
    )
    await db_session.commit()

    # Override status to "away" via PATCH.
    patch_resp = await client.patch(
        f"/v1/agents/{a['id']}", json={"status": "away"}, headers=auth_headers
    )
    assert patch_resp.status_code == 200

    resp = await client.get("/v1/agents/roster", headers=auth_headers)
    by_name = {x["name"]: x for x in resp.json()["agents"]}
    assert by_name["override-away"]["status"] == "away"
    assert by_name["override-away"]["online"] is False


@pytest.mark.asyncio
async def test_roster_excludes_soft_deleted(client, auth_headers):
    keep = await _create_agent(client, auth_headers, "roster-keep")
    drop = await _create_agent(client, auth_headers, "roster-drop")

    del_resp = await client.delete(f"/v1/agents/{drop['id']}", headers=auth_headers)
    assert del_resp.status_code == 204

    resp = await client.get("/v1/agents/roster", headers=auth_headers)
    names = {a["name"] for a in resp.json()["agents"]}
    assert "roster-keep" in names
    assert "roster-drop" not in names


@pytest.mark.asyncio
async def test_roster_preferred_contact_derivation(client, auth_headers):
    poll_only = await _create_agent(client, auth_headers, "pc-poll")
    push_capable = await _create_agent(
        client, auth_headers, "pc-push", webhook_url="https://example.com/wh"
    )

    resp = await client.get("/v1/agents/roster", headers=auth_headers)
    by_name = {a["name"]: a for a in resp.json()["agents"]}
    assert by_name["pc-poll"]["preferred_contact"] == "async"
    assert by_name["pc-push"]["preferred_contact"] == "sync"


@pytest.mark.asyncio
async def test_last_seen_relative_formatting(client, auth_headers, db_session):
    a = await _create_agent(client, auth_headers, "rel-test")
    now = datetime.now(timezone.utc)

    cases = [
        (timedelta(seconds=30), "active now"),
        (timedelta(minutes=5), "5m ago"),
        (timedelta(hours=2), "2h ago"),
    ]

    for delta, expected in cases:
        await db_session.execute(
            update(Agent).where(Agent.id == a["id"]).values(last_seen_at=now - delta)
        )
        await db_session.commit()
        resp = await client.get("/v1/agents/roster", headers=auth_headers)
        entry = next(x for x in resp.json()["agents"] if x["name"] == "rel-test")
        assert entry["last_seen_relative"] == expected, f"delta={delta}"

    # Never seen.
    await db_session.execute(
        update(Agent).where(Agent.id == a["id"]).values(last_seen_at=None)
    )
    await db_session.commit()
    resp = await client.get("/v1/agents/roster", headers=auth_headers)
    entry = next(x for x in resp.json()["agents"] if x["name"] == "rel-test")
    assert entry["last_seen_relative"] == "never"


@pytest.mark.asyncio
async def test_roster_etag_304_when_unchanged(client, auth_headers, db_session):
    """ETag round-trip: first GET returns 200 + ETag; second GET with
    matching If-None-Match returns 304 with no body."""
    a = await _create_agent(client, auth_headers, "etag-stable")

    # Pin last_seen_at to a deterministic past value so the bucket is stable.
    now = datetime.now(timezone.utc)
    await db_session.execute(
        update(Agent).where(Agent.id == a["id"]).values(last_seen_at=now - timedelta(seconds=30))
    )
    await db_session.commit()

    first = await client.get("/v1/agents/roster", headers=auth_headers)
    assert first.status_code == 200
    etag = first.headers.get("etag")
    assert etag is not None
    assert etag.startswith('W/"')

    second = await client.get(
        "/v1/agents/roster",
        headers={**auth_headers, "If-None-Match": etag},
    )
    assert second.status_code == 304
    assert second.headers.get("etag") == etag


@pytest.mark.asyncio
async def test_roster_etag_changes_when_agent_added(client, auth_headers, db_session):
    """A new agent flips the ETag — clients can rely on conditional GET."""
    await _create_agent(client, auth_headers, "etag-base")

    first = await client.get("/v1/agents/roster", headers=auth_headers)
    etag_before = first.headers.get("etag")
    assert etag_before is not None

    await _create_agent(client, auth_headers, "etag-new-agent")

    second = await client.get(
        "/v1/agents/roster",
        headers={**auth_headers, "If-None-Match": etag_before},
    )
    assert second.status_code == 200
    etag_after = second.headers.get("etag")
    assert etag_after != etag_before
