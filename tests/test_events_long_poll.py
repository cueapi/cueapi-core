"""Tests for `?wait=long` long-poll mode on GET /v1/agents/{ref}/events.

Closes Q1 from the PR-1b spec; deferred from PR-1b for clean-ship
discipline (CTO call 2026-05-11). Backlog row cmp0jjz7c.

Verifies:

* Short-poll (default) returns immediately with whatever is queued.
* Long-poll with events already available returns immediately
  (doesn't wait when there's something to deliver).
* Long-poll with no events times out cleanly and returns 200 with
  empty events list.
* Long-poll polls internally and returns ASAP when a new event
  arrives during the wait window.
* Long-poll respects the ``since`` cursor — events with id <=
  since are not surfaced even during the wait window.

Uses monkeypatching of the LONG_POLL_MAX_SECONDS constant to keep
tests fast (1-2s max instead of 30s).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.event import Event
from app.models.user import User
from app.routers import events as events_router
from app.routers.events import _run_long_poll_wait


async def _resolve_user_id(db_session, email):
    user = (
        await db_session.execute(select(User).where(User.email == email))
    ).scalar_one()
    return str(user.id)


@pytest_asyncio.fixture
async def lp_agent(db_session: AsyncSession, registered_user: dict) -> Agent:
    user_id = await _resolve_user_id(db_session, registered_user["email"])
    agent = Agent(
        id="agt_longpoll0001",
        user_id=user_id,
        slug="longpoll",
        display_name="Long-poll Test Agent",
    )
    db_session.add(agent)
    await db_session.commit()
    await db_session.refresh(agent)
    return agent


@pytest.fixture
def fast_long_poll(monkeypatch):
    """Shrink the long-poll window for fast tests."""
    monkeypatch.setattr(events_router, "LONG_POLL_MAX_SECONDS", 1.0)
    monkeypatch.setattr(
        events_router, "LONG_POLL_INTERNAL_INTERVAL_SECONDS", 0.1
    )


# ───────────────────────────────────────────────────────────────────────
# Short-poll (default) — unchanged behavior
# ───────────────────────────────────────────────────────────────────────


async def test_short_poll_default_returns_immediately(
    client: AsyncClient, auth_headers: dict, lp_agent: Agent
):
    """No `wait` query param + empty events → immediate empty response."""
    started = asyncio.get_event_loop().time()
    resp = await client.get(
        f"/v1/agents/{lp_agent.id}/events",
        headers=auth_headers,
    )
    elapsed = asyncio.get_event_loop().time() - started
    assert resp.status_code == 200
    assert resp.json()["events"] == []
    # Should NOT block — < 0.5s is generous.
    assert elapsed < 0.5


# ───────────────────────────────────────────────────────────────────────
# Long-poll happy path
# ───────────────────────────────────────────────────────────────────────


async def test_long_poll_with_existing_events_returns_immediately(
    client: AsyncClient,
    auth_headers: dict,
    lp_agent: Agent,
    db_session: AsyncSession,
    fast_long_poll,
):
    """Long-poll only blocks when there's nothing to deliver. If
    events already exist, return immediately."""
    db_session.add(
        Event(
            event_type="message.delivered",
            recipient_agent_id=lp_agent.id,
            payload={"k": "v"},
            emitted_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    started = asyncio.get_event_loop().time()
    resp = await client.get(
        f"/v1/agents/{lp_agent.id}/events?wait=long",
        headers=auth_headers,
    )
    elapsed = asyncio.get_event_loop().time() - started
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["events"]) == 1
    # No block since data was available immediately.
    assert elapsed < 0.5


async def test_long_poll_no_events_times_out_cleanly(
    client: AsyncClient,
    auth_headers: dict,
    lp_agent: Agent,
    fast_long_poll,
):
    """Long-poll with no events for the full wait window → 200 with
    empty events array (NOT a 408 or 504). The contract is: return
    eventually, even if empty."""
    started = asyncio.get_event_loop().time()
    resp = await client.get(
        f"/v1/agents/{lp_agent.id}/events?wait=long",
        headers=auth_headers,
    )
    elapsed = asyncio.get_event_loop().time() - started
    assert resp.status_code == 200
    body = resp.json()
    assert body["events"] == []
    assert body["next_cursor"] is None
    assert body["has_more"] is False
    # Waited approximately the full LONG_POLL_MAX_SECONDS (1.0s in
    # fast_long_poll fixture). Allow some slack for asyncio scheduling.
    assert 0.8 < elapsed < 2.0


async def test_long_poll_returns_asap_on_new_event(
    client: AsyncClient,
    auth_headers: dict,
    lp_agent: Agent,
    db_session: AsyncSession,
    fast_long_poll,
):
    """While long-poll is waiting, a new event arriving should be
    picked up within ~poll_interval seconds — well before the 1s
    timeout."""

    async def insert_event_after_delay():
        """Background task: wait 200ms then insert an event."""
        await asyncio.sleep(0.2)
        # Use a fresh session since the main request holds its own
        # session via the dependency.
        from app.database import async_session

        async with async_session() as fresh_session:
            fresh_session.add(
                Event(
                    event_type="message.delivered",
                    recipient_agent_id=lp_agent.id,
                    payload={"late_arrival": True},
                    emitted_at=datetime.now(timezone.utc),
                )
            )
            await fresh_session.commit()

    inserter = asyncio.create_task(insert_event_after_delay())

    started = asyncio.get_event_loop().time()
    resp = await client.get(
        f"/v1/agents/{lp_agent.id}/events?wait=long",
        headers=auth_headers,
    )
    elapsed = asyncio.get_event_loop().time() - started

    await inserter  # ensure cleanup

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["events"]) == 1
    assert body["events"][0]["payload"]["late_arrival"] is True
    # Should return well before the 1s full timeout — insert was
    # at 200ms + poll interval is 100ms, so first re-poll after
    # insert is ~300ms. Allow up to 800ms for scheduling slack.
    assert elapsed < 0.9, f"long-poll took {elapsed}s; expected < 0.9"


async def test_long_poll_respects_since_cursor(
    client: AsyncClient,
    auth_headers: dict,
    lp_agent: Agent,
    db_session: AsyncSession,
    fast_long_poll,
):
    """Events with id <= since cursor are not surfaced during the
    wait window — long-poll should time out if all available events
    are below the cursor."""
    db_session.add(
        Event(
            event_type="message.delivered",
            recipient_agent_id=lp_agent.id,
            payload={"old": True},
            emitted_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()
    # Seed event has some id N; pass since=N to skip it. Find N
    # by querying.
    existing = (
        await db_session.execute(
            select(Event).where(Event.recipient_agent_id == lp_agent.id)
        )
    ).scalar_one()
    high_cursor = existing.id

    started = asyncio.get_event_loop().time()
    resp = await client.get(
        f"/v1/agents/{lp_agent.id}/events?wait=long&since={high_cursor}",
        headers=auth_headers,
    )
    elapsed = asyncio.get_event_loop().time() - started
    assert resp.status_code == 200
    # The existing event has id <= since, so it's not surfaced.
    # Long-poll times out empty.
    assert resp.json()["events"] == []
    assert 0.8 < elapsed < 2.0


# ───────────────────────────────────────────────────────────────────────
# Schema validation — wait param value
# ───────────────────────────────────────────────────────────────────────


async def test_invalid_wait_value_rejected(
    client: AsyncClient, auth_headers: dict, lp_agent: Agent
):
    """wait=anything-other-than-long returns 422 (FastAPI
    Literal['long']) enforces)."""
    resp = await client.get(
        f"/v1/agents/{lp_agent.id}/events?wait=forever",
        headers=auth_headers,
    )
    assert resp.status_code == 422


async def test_wait_omitted_works_as_before(
    client: AsyncClient,
    auth_headers: dict,
    lp_agent: Agent,
    db_session: AsyncSession,
):
    """No wait param at all (omitted entirely) is identical to
    short-poll — back-compat sanity check."""
    db_session.add(
        Event(
            event_type="message.delivered",
            recipient_agent_id=lp_agent.id,
            payload={"happy_short_poll": True},
            emitted_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()
    resp = await client.get(
        f"/v1/agents/{lp_agent.id}/events",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert len(resp.json()["events"]) == 1


# ───────────────────────────────────────────────────────────────────────
# Direct unit tests for _run_long_poll_wait — pytest-cov reliably
# traces branches when the helper is called directly (not via ASGI).
# Pattern from CLAUDE.md patch-coverage discipline.
# ───────────────────────────────────────────────────────────────────────


async def test_helper_returns_empty_on_timeout(
    db_session: AsyncSession,
    lp_agent: Agent,
    fast_long_poll,
):
    """No events emitted during the window → helper returns empty
    after LONG_POLL_MAX_SECONDS elapses."""
    started = asyncio.get_event_loop().time()
    events, cursor, has_more = await _run_long_poll_wait(
        db_session,
        recipient_agent_id=lp_agent.id,
        since=0,
        limit=100,
        event_type=None,
    )
    elapsed = asyncio.get_event_loop().time() - started
    assert events == []
    assert cursor is None
    assert has_more is False
    # ~LONG_POLL_MAX_SECONDS (1.0s in fast_long_poll). Allow slack.
    assert 0.8 < elapsed < 2.0


async def test_helper_returns_asap_on_event_arrival(
    db_session: AsyncSession,
    lp_agent: Agent,
    fast_long_poll,
):
    """Event inserted mid-wait → helper picks it up + returns before
    the window elapses. Covers the `if events: break` branch."""
    async def insert_after_delay():
        await asyncio.sleep(0.2)
        from app.database import async_session
        async with async_session() as fresh:
            fresh.add(
                Event(
                    event_type="message.delivered",
                    recipient_agent_id=lp_agent.id,
                    payload={"arrived_during_wait": True},
                    emitted_at=datetime.now(timezone.utc),
                )
            )
            await fresh.commit()

    inserter = asyncio.create_task(insert_after_delay())
    started = asyncio.get_event_loop().time()
    events, cursor, has_more = await _run_long_poll_wait(
        db_session,
        recipient_agent_id=lp_agent.id,
        since=0,
        limit=100,
        event_type=None,
    )
    elapsed = asyncio.get_event_loop().time() - started
    await inserter

    assert len(events) == 1
    assert events[0].payload["arrived_during_wait"] is True
    assert cursor == events[0].id
    # Insert at 200ms + poll cadence ~100ms → return < 500ms; well
    # before the 1s timeout.
    assert elapsed < 0.9


async def test_helper_respects_event_type_filter(
    db_session: AsyncSession,
    lp_agent: Agent,
    fast_long_poll,
):
    """Helper passes event_type through to pull_events. An event
    of a different type doesn't satisfy the wait."""
    # Seed an event of a TYPE that DOESN'T match the filter.
    # NOTE: pull_events doesn't validate event_type against the
    # KNOWN_EVENT_TYPES registry (that check is at subscription
    # create time), so we can seed an arbitrary type.
    db_session.add(
        Event(
            event_type="some.other.type",
            recipient_agent_id=lp_agent.id,
            payload={},
            emitted_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    started = asyncio.get_event_loop().time()
    events, cursor, has_more = await _run_long_poll_wait(
        db_session,
        recipient_agent_id=lp_agent.id,
        since=0,
        limit=100,
        event_type="message.delivered",  # filter excludes the seeded event
    )
    elapsed = asyncio.get_event_loop().time() - started
    # No matching event → timeout empty.
    assert events == []
    assert 0.8 < elapsed < 2.0


async def test_helper_respects_since_cursor(
    db_session: AsyncSession,
    lp_agent: Agent,
    fast_long_poll,
):
    """Helper passes `since` through to pull_events. Events with
    id <= since are not surfaced."""
    db_session.add(
        Event(
            event_type="message.delivered",
            recipient_agent_id=lp_agent.id,
            payload={"old": True},
            emitted_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()
    existing = (
        await db_session.execute(
            select(Event).where(Event.recipient_agent_id == lp_agent.id)
        )
    ).scalar_one()

    started = asyncio.get_event_loop().time()
    events, _, _ = await _run_long_poll_wait(
        db_session,
        recipient_agent_id=lp_agent.id,
        since=existing.id,  # skip the existing event
        limit=100,
        event_type=None,
    )
    elapsed = asyncio.get_event_loop().time() - started
    assert events == []
    assert 0.8 < elapsed < 2.0
