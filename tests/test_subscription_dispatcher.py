"""Tests for ``worker/subscription_dispatcher.py``.

Pure-helper tests (no DB / no HTTP):
* ``_serialize_event`` — wire shape stability
* ``_build_webhook_body`` — delivery_id format, events array
* ``_should_trip_breaker`` — boundary at the 10-failure threshold
* ``_classify_response`` — 2xx/5xx/408/429/4xx-other branches

Integration tests (DB):
* ``cleanup_old_events`` — deletes old events, leaves fresh ones
* ``dispatch_subscription_events`` — skips when no subs; advances
  watermark on 200; bumps failures on 500; trips circuit breaker
  after 10 consecutive failures

HTTP calls are stubbed via httpx mock transport.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.models.agent import Agent
from app.models.event import Event
from app.models.subscription import Subscription
from app.models.user import User
from worker.subscription_dispatcher import (
    CIRCUIT_BREAKER_THRESHOLD,
    _build_webhook_body,
    _classify_response,
    _serialize_event,
    _should_trip_breaker,
    cleanup_old_events,
    dispatch_subscription_events,
)


# ───────────────────────────────────────────────────────────────────────
# Pure-helper unit tests
# ───────────────────────────────────────────────────────────────────────

def test_serialize_event_shape():
    ev = Event(
        id=42,
        event_type="message.delivered",
        recipient_agent_id="agt_x",
        payload={"k": "v"},
        emitted_at=datetime(2026, 5, 11, 1, 0, tzinfo=timezone.utc),
    )
    out = _serialize_event(ev)
    assert out == {
        "id": 42,
        "event_type": "message.delivered",
        "payload": {"k": "v"},
        "emitted_at": "2026-05-11T01:00:00+00:00",
    }


def test_serialize_event_handles_null_payload():
    ev = Event(
        id=1,
        event_type="x",
        recipient_agent_id="agt_x",
        payload=None,
        emitted_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
    )
    out = _serialize_event(ev)
    assert out["payload"] == {}


def test_build_webhook_body_delivery_id_deterministic():
    """delivery_id derived from sub_id + first/last event ids — same
    inputs → same id (lets recipient dedup repeats)."""
    events = [
        Event(id=10, event_type="x", recipient_agent_id="agt_x",
              payload={}, emitted_at=datetime.now(timezone.utc)),
        Event(id=15, event_type="x", recipient_agent_id="agt_x",
              payload={}, emitted_at=datetime.now(timezone.utc)),
    ]
    body = _build_webhook_body("sub-xyz", events)
    assert body["delivery_id"] == "dlv_sub-xyz_10_15"
    assert body["subscription_id"] == "sub-xyz"
    assert len(body["events"]) == 2


def test_build_webhook_body_empty_events():
    """Empty events list still produces a valid body — used as a
    defensive code path; production callers filter empty before
    calling."""
    body = _build_webhook_body("sub-xyz", [])
    assert body["events"] == []
    assert body["delivery_id"] == "dlv_sub-xyz_0_0"


def test_should_trip_breaker_at_threshold():
    """Boundary case — fires AT threshold, not just above."""
    assert _should_trip_breaker(CIRCUIT_BREAKER_THRESHOLD) is True
    assert _should_trip_breaker(CIRCUIT_BREAKER_THRESHOLD - 1) is False
    assert _should_trip_breaker(CIRCUIT_BREAKER_THRESHOLD + 5) is True


def test_classify_response_success():
    assert _classify_response(ok=True, status_code=200) == "success"
    assert _classify_response(ok=True, status_code=204) == "success"


def test_classify_response_retry_on_5xx():
    assert _classify_response(ok=False, status_code=500) == "retry"
    assert _classify_response(ok=False, status_code=502) == "retry"
    assert _classify_response(ok=False, status_code=503) == "retry"


def test_classify_response_retry_on_408_429():
    """Timeout + rate-limit-ish status codes use retry semantics
    per spec §Failure handling."""
    assert _classify_response(ok=False, status_code=408) == "retry"
    assert _classify_response(ok=False, status_code=429) == "retry"


def test_classify_response_skip_on_4xx_other():
    """Caller-side errors (404, 401, 400) bump failures but events
    will keep failing until caller fixes their webhook."""
    assert _classify_response(ok=False, status_code=400) == "skip"
    assert _classify_response(ok=False, status_code=401) == "skip"
    assert _classify_response(ok=False, status_code=404) == "skip"


def test_classify_response_retry_on_network_error():
    """Network error path: status_code=0 (httpx exception caught)."""
    assert _classify_response(ok=False, status_code=0) == "retry"


# ───────────────────────────────────────────────────────────────────────
# Integration: cleanup_old_events
# ───────────────────────────────────────────────────────────────────────

async def _resolve_user_id(db_session, email):
    user = (
        await db_session.execute(select(User).where(User.email == email))
    ).scalar_one()
    return str(user.id)


@pytest_asyncio.fixture
async def disp_agent(db_session, registered_user):
    user_id = await _resolve_user_id(db_session, registered_user["email"])
    agent = Agent(
        id="agt_disp00000001",
        user_id=user_id,
        slug="disp",
        display_name="Dispatcher Test",
    )
    db_session.add(agent)
    await db_session.commit()
    await db_session.refresh(agent)
    return agent


async def _make_engine_from_session_factory():
    """Build an engine pointing at the same test DB the conftest uses.

    The dispatcher's API takes ``AsyncEngine`` (it manages its own
    connections via ``async with engine.begin()``). The conftest's
    ``db_session`` is a Session, not an Engine — so we need a fresh
    engine. We can reuse the engine ``test_session`` was created
    against via the conftest's TEST_DATABASE_URL settings.
    """
    from sqlalchemy.ext.asyncio import create_async_engine
    from app.config import settings
    return create_async_engine(settings.async_database_url)


async def test_cleanup_old_events_deletes_old_keeps_fresh(
    db_session, disp_agent
):
    """Old events (>7 days) deleted; fresh ones preserved."""
    now = datetime.now(timezone.utc)
    old = Event(
        event_type="message.delivered",
        recipient_agent_id=disp_agent.id,
        payload={"age": "old"},
        emitted_at=now - timedelta(days=10),
    )
    fresh = Event(
        event_type="message.delivered",
        recipient_agent_id=disp_agent.id,
        payload={"age": "fresh"},
        emitted_at=now - timedelta(hours=1),
    )
    db_session.add(old)
    db_session.add(fresh)
    await db_session.commit()

    engine = await _make_engine_from_session_factory()
    try:
        deleted = await cleanup_old_events(engine, retention_days=7)
    finally:
        await engine.dispose()

    assert deleted == 1

    # fresh still present.
    remaining = (
        await db_session.execute(
            select(Event).where(Event.recipient_agent_id == disp_agent.id)
        )
    ).scalars().all()
    assert len(remaining) == 1
    assert remaining[0].payload == {"age": "fresh"}


async def test_cleanup_old_events_returns_zero_when_nothing_old(
    db_session, disp_agent
):
    fresh = Event(
        event_type="message.delivered",
        recipient_agent_id=disp_agent.id,
        payload={},
        emitted_at=datetime.now(timezone.utc),
    )
    db_session.add(fresh)
    await db_session.commit()

    engine = await _make_engine_from_session_factory()
    try:
        deleted = await cleanup_old_events(engine, retention_days=7)
    finally:
        await engine.dispose()
    assert deleted == 0


# ───────────────────────────────────────────────────────────────────────
# Integration: dispatch_subscription_events
# ───────────────────────────────────────────────────────────────────────

async def test_dispatch_returns_zero_when_no_subs(db_session, disp_agent):
    engine = await _make_engine_from_session_factory()
    try:
        attempts = await dispatch_subscription_events(engine)
    finally:
        await engine.dispose()
    assert attempts == 0


async def test_dispatch_advances_watermark_on_2xx(
    db_session, disp_agent
):
    """Happy path: pending event + webhook sub + 2xx response → watermark
    bumps to event.id; consecutive_failures stays 0."""
    sub = Subscription(
        subscriber_agent_id=disp_agent.id,
        event_type="message.delivered",
        delivery_target="webhook",
        webhook_url="https://example.com/hook",
        webhook_secret="whsec_test",
    )
    db_session.add(sub)
    ev = Event(
        event_type="message.delivered",
        recipient_agent_id=disp_agent.id,
        payload={"k": "v"},
        emitted_at=datetime.now(timezone.utc),
    )
    db_session.add(ev)
    await db_session.commit()
    sub_id = sub.id
    ev_id = ev.id

    # Stub httpx so we never actually leave the test process.
    async def fake_deliver(*, url, secret, body):
        return (True, 200)

    engine = await _make_engine_from_session_factory()
    try:
        with patch(
            "worker.subscription_dispatcher._deliver_webhook",
            side_effect=fake_deliver,
        ):
            attempts = await dispatch_subscription_events(engine)
    finally:
        await engine.dispose()
    assert attempts == 1

    refreshed = (
        await db_session.execute(
            select(Subscription).where(Subscription.id == sub_id)
        )
    ).scalar_one()
    # SQLAlchemy needs an explicit refresh to see the engine's
    # updates in the other session.
    await db_session.refresh(refreshed)
    assert refreshed.last_dispatched_event_id == ev_id
    assert refreshed.consecutive_failures == 0


async def test_dispatch_bumps_failures_on_5xx(db_session, disp_agent):
    sub = Subscription(
        subscriber_agent_id=disp_agent.id,
        event_type="message.delivered",
        delivery_target="webhook",
        webhook_url="https://example.com/hook",
        webhook_secret="whsec_test",
    )
    db_session.add(sub)
    ev = Event(
        event_type="message.delivered",
        recipient_agent_id=disp_agent.id,
        payload={},
        emitted_at=datetime.now(timezone.utc),
    )
    db_session.add(ev)
    await db_session.commit()
    sub_id = sub.id

    async def fake_deliver(*, url, secret, body):
        return (False, 500)

    engine = await _make_engine_from_session_factory()
    try:
        with patch(
            "worker.subscription_dispatcher._deliver_webhook",
            side_effect=fake_deliver,
        ):
            attempts = await dispatch_subscription_events(engine)
    finally:
        await engine.dispose()
    assert attempts == 1

    refreshed = (
        await db_session.execute(
            select(Subscription).where(Subscription.id == sub_id)
        )
    ).scalar_one()
    await db_session.refresh(refreshed)
    # Watermark NOT advanced; failure counter bumped.
    assert refreshed.last_dispatched_event_id is None
    assert refreshed.consecutive_failures == 1
    assert refreshed.paused_until is None  # not yet at threshold


async def test_dispatch_trips_circuit_breaker_at_threshold(
    db_session, disp_agent
):
    """At threshold-1 + one more failure → trips breaker."""
    # Start with consecutive_failures already at threshold-1.
    sub = Subscription(
        subscriber_agent_id=disp_agent.id,
        event_type="message.delivered",
        delivery_target="webhook",
        webhook_url="https://example.com/hook",
        webhook_secret="whsec_test",
        consecutive_failures=CIRCUIT_BREAKER_THRESHOLD - 1,
    )
    db_session.add(sub)
    ev = Event(
        event_type="message.delivered",
        recipient_agent_id=disp_agent.id,
        payload={},
        emitted_at=datetime.now(timezone.utc),
    )
    db_session.add(ev)
    await db_session.commit()
    sub_id = sub.id

    async def fake_deliver(*, url, secret, body):
        return (False, 503)

    engine = await _make_engine_from_session_factory()
    try:
        with patch(
            "worker.subscription_dispatcher._deliver_webhook",
            side_effect=fake_deliver,
        ):
            await dispatch_subscription_events(engine)
    finally:
        await engine.dispose()

    refreshed = (
        await db_session.execute(
            select(Subscription).where(Subscription.id == sub_id)
        )
    ).scalar_one()
    await db_session.refresh(refreshed)
    assert refreshed.consecutive_failures == CIRCUIT_BREAKER_THRESHOLD
    assert refreshed.paused_until is not None
    # paused_until is ~1h in the future.
    delta = refreshed.paused_until - datetime.now(timezone.utc)
    assert 3500 < delta.total_seconds() < 3700


async def test_dispatch_skips_paused_subs(db_session, disp_agent):
    """Subs with paused_until > NOW are not considered dispatch-due."""
    future = datetime.now(timezone.utc) + timedelta(minutes=30)
    sub = Subscription(
        subscriber_agent_id=disp_agent.id,
        event_type="message.delivered",
        delivery_target="webhook",
        webhook_url="https://example.com/hook",
        webhook_secret="whsec_test",
        consecutive_failures=CIRCUIT_BREAKER_THRESHOLD,
        paused_until=future,
    )
    db_session.add(sub)
    ev = Event(
        event_type="message.delivered",
        recipient_agent_id=disp_agent.id,
        payload={},
        emitted_at=datetime.now(timezone.utc),
    )
    db_session.add(ev)
    await db_session.commit()

    deliver_calls = []

    async def fake_deliver(*, url, secret, body):
        deliver_calls.append(url)
        return (True, 200)

    engine = await _make_engine_from_session_factory()
    try:
        with patch(
            "worker.subscription_dispatcher._deliver_webhook",
            side_effect=fake_deliver,
        ):
            attempts = await dispatch_subscription_events(engine)
    finally:
        await engine.dispose()
    # Paused sub not picked up.
    assert attempts == 0
    assert deliver_calls == []


async def test_dispatch_skips_detached_subs(db_session, disp_agent):
    """Detached subs don't appear in the dispatch query."""
    sub = Subscription(
        subscriber_agent_id=disp_agent.id,
        event_type="message.delivered",
        delivery_target="webhook",
        webhook_url="https://example.com/hook",
        webhook_secret="whsec_test",
        detached_at=datetime.now(timezone.utc),
    )
    db_session.add(sub)
    ev = Event(
        event_type="message.delivered",
        recipient_agent_id=disp_agent.id,
        payload={},
        emitted_at=datetime.now(timezone.utc),
    )
    db_session.add(ev)
    await db_session.commit()

    async def fake_deliver(*, url, secret, body):
        return (True, 200)

    engine = await _make_engine_from_session_factory()
    try:
        with patch(
            "worker.subscription_dispatcher._deliver_webhook",
            side_effect=fake_deliver,
        ):
            attempts = await dispatch_subscription_events(engine)
    finally:
        await engine.dispose()
    assert attempts == 0
