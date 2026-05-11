"""PR-2a tests: messaging-emission wiring of the event-emit primitive.

Verifies:

* Migration 059's 3 new columns (dispatch_priority_bucket,
  message_dispatch_error, correlation_id) are present and have the
  documented defaults.
* `correlation_id` round-trips through MessageCreate → DB →
  MessageResponse.
* `dispatch_priority_bucket` is computed from `priority` at create
  time.
* `create_message` emits a ``message.delivered`` event on the
  event-emit primitive substrate (PR-1b). Subscribers in
  cueapi-presence-runtime v0.2 can pull this event via
  ``GET /v1/agents/{ref}/events``.
* Emission is idempotent on ``(event_type, message_id)`` so retries
  of create_message after transient failure don't double-emit.
* Emission failures are non-blocking (best-effort) — message-create
  succeeds even if events_service raises.

End-to-end via the FastAPI test client + direct DB inspection.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.event import Event
from app.models.message import Message
from app.models.user import User


async def _resolve_user_id(db_session: AsyncSession, email: str) -> str:
    user = (
        await db_session.execute(select(User).where(User.email == email))
    ).scalar_one()
    return str(user.id)


@pytest_asyncio.fixture
async def sender_agent(db_session: AsyncSession, registered_user: dict) -> Agent:
    user_id = await _resolve_user_id(db_session, registered_user["email"])
    agent = Agent(
        id="agt_pr2asender01",
        user_id=user_id,
        slug="pr2a-sender",
        display_name="PR2a Sender",
    )
    db_session.add(agent)
    await db_session.commit()
    await db_session.refresh(agent)
    return agent


@pytest_asyncio.fixture
async def recipient_agent(db_session: AsyncSession, registered_user: dict) -> Agent:
    user_id = await _resolve_user_id(db_session, registered_user["email"])
    agent = Agent(
        id="agt_pr2arecv0001",
        user_id=user_id,
        slug="pr2a-recv",
        display_name="PR2a Recipient",
    )
    db_session.add(agent)
    await db_session.commit()
    await db_session.refresh(agent)
    return agent


# ───────────────────────────────────────────────────────────────────────
# Schema: correlation_id round-trip + dispatch_priority_bucket
# ───────────────────────────────────────────────────────────────────────


async def test_correlation_id_roundtrips_through_create(
    client: AsyncClient,
    auth_headers: dict,
    sender_agent: Agent,
    recipient_agent: Agent,
    db_session: AsyncSession,
):
    """POST /v1/messages with correlation_id stores it + surfaces it
    back on the response."""
    resp = await client.post(
        "/v1/messages",
        json={
            "to": recipient_agent.id,
            "body": "rpc request",
            "correlation_id": "req_xyz_001",
        },
        headers={**auth_headers, "X-CueAPI-From-Agent": sender_agent.id},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["correlation_id"] == "req_xyz_001"
    # Also persisted in DB.
    msg_id = body["id"]
    refreshed = (
        await db_session.execute(select(Message).where(Message.id == msg_id))
    ).scalar_one()
    assert refreshed.correlation_id == "req_xyz_001"


async def test_correlation_id_optional_defaults_null(
    client: AsyncClient,
    auth_headers: dict,
    sender_agent: Agent,
    recipient_agent: Agent,
):
    """Existing senders that don't supply correlation_id get null
    (backward-compat)."""
    resp = await client.post(
        "/v1/messages",
        json={"to": recipient_agent.id, "body": "no rpc field"},
        headers={**auth_headers, "X-CueAPI-From-Agent": sender_agent.id},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["correlation_id"] is None


async def test_dispatch_priority_bucket_computed_from_priority(
    client: AsyncClient,
    auth_headers: dict,
    sender_agent: Agent,
    recipient_agent: Agent,
    db_session: AsyncSession,
):
    """create_message stamps dispatch_priority_bucket = priority.
    v1 = verbatim; future tier policy reads this column."""
    resp = await client.post(
        "/v1/messages",
        json={
            "to": recipient_agent.id,
            "body": "high-priority",
            "priority": 5,
        },
        headers={**auth_headers, "X-CueAPI-From-Agent": sender_agent.id},
    )
    assert resp.status_code == 201
    body = resp.json()
    # Server-side anti-abuse can downgrade priority > 3; verify both
    # surfaces (priority + bucket) agree post-create.
    assert body["dispatch_priority_bucket"] == body["priority"]


async def test_message_dispatch_error_starts_null(
    client: AsyncClient,
    auth_headers: dict,
    sender_agent: Agent,
    recipient_agent: Agent,
):
    """On create, message_dispatch_error is null (it's populated only
    when a downstream handler reports outcome.error after the bridge
    fires)."""
    resp = await client.post(
        "/v1/messages",
        json={"to": recipient_agent.id, "body": "happy path"},
        headers={**auth_headers, "X-CueAPI-From-Agent": sender_agent.id},
    )
    assert resp.status_code == 201
    assert resp.json()["message_dispatch_error"] is None


# ───────────────────────────────────────────────────────────────────────
# Event emission — substrate wiring
# ───────────────────────────────────────────────────────────────────────


async def test_create_message_emits_delivered_event(
    client: AsyncClient,
    auth_headers: dict,
    sender_agent: Agent,
    recipient_agent: Agent,
    db_session: AsyncSession,
):
    """POST /v1/messages emits a `message.delivered` event for the
    recipient — subscribers can pull it via GET
    /v1/agents/{ref}/events."""
    resp = await client.post(
        "/v1/messages",
        json={
            "to": recipient_agent.id,
            "body": "should produce event",
            "subject": "hello",
        },
        headers={**auth_headers, "X-CueAPI-From-Agent": sender_agent.id},
    )
    assert resp.status_code == 201
    msg_id = resp.json()["id"]

    # Pull events for recipient via direct DB query (avoids needing
    # the agent's API key for the /events endpoint).
    events = (
        await db_session.execute(
            select(Event).where(
                Event.recipient_agent_id == recipient_agent.id,
                Event.event_type == "message.delivered",
            )
        )
    ).scalars().all()
    assert len(events) == 1
    ev = events[0]
    assert ev.payload["message_id"] == msg_id
    assert ev.payload["sender_agent_id"] == sender_agent.id
    assert ev.payload["recipient_agent_id"] == recipient_agent.id
    assert ev.payload["subject"] == "hello"
    # idempotency_key set to message.delivered:<msg_id>
    assert ev.idempotency_key == f"message.delivered:{msg_id}"


async def test_event_payload_includes_correlation_id(
    client: AsyncClient,
    auth_headers: dict,
    sender_agent: Agent,
    recipient_agent: Agent,
    db_session: AsyncSession,
):
    """correlation_id flows through the event payload so subscribers
    can match request/response programmatically."""
    resp = await client.post(
        "/v1/messages",
        json={
            "to": recipient_agent.id,
            "body": "rpc",
            "correlation_id": "req_abc_999",
        },
        headers={**auth_headers, "X-CueAPI-From-Agent": sender_agent.id},
    )
    assert resp.status_code == 201

    ev = (
        await db_session.execute(
            select(Event).where(Event.event_type == "message.delivered")
        )
    ).scalar_one()
    assert ev.payload["correlation_id"] == "req_abc_999"


async def test_event_emission_idempotent_on_message_id(
    client: AsyncClient,
    auth_headers: dict,
    sender_agent: Agent,
    recipient_agent: Agent,
    db_session: AsyncSession,
):
    """idempotency_key=`message.delivered:<msg_id>` prevents
    double-emit if create_message somehow runs twice for the same
    message id (e.g., retry-after-transient-failure)."""
    from app.services.events_service import emit_event

    fake_msg_id = "msg_dummytest01"
    # First emit.
    first = await emit_event(
        db_session,
        event_type="message.delivered",
        recipient_agent_id=recipient_agent.id,
        payload={"message_id": fake_msg_id, "v": 1},
        idempotency_key=f"message.delivered:{fake_msg_id}",
    )
    await db_session.commit()

    # Second emit (simulating retry after transient).
    second = await emit_event(
        db_session,
        event_type="message.delivered",
        recipient_agent_id=recipient_agent.id,
        payload={"message_id": fake_msg_id, "v": 2},
        idempotency_key=f"message.delivered:{fake_msg_id}",
    )
    await db_session.commit()

    # Same row returned; original payload preserved.
    assert second.id == first.id

    # Only one event in the DB for this idempotency_key.
    all_for_key = (
        await db_session.execute(
            select(Event).where(
                Event.idempotency_key == f"message.delivered:{fake_msg_id}"
            )
        )
    ).scalars().all()
    assert len(all_for_key) == 1


async def test_event_emission_failure_is_non_blocking(
    client: AsyncClient,
    auth_headers: dict,
    sender_agent: Agent,
    recipient_agent: Agent,
    db_session: AsyncSession,
):
    """If events_service.emit_event raises (e.g., DB blip on events
    table), create_message still succeeds. The events_service call
    is wrapped in try/except — best-effort telemetry, not blocking."""
    async def fake_emit_raises(*args, **kwargs):
        raise RuntimeError("events table unavailable (simulated)")

    with patch(
        "app.services.message_service.emit_event",
        side_effect=fake_emit_raises,
    ):
        resp = await client.post(
            "/v1/messages",
            json={"to": recipient_agent.id, "body": "should still send"},
            headers={**auth_headers, "X-CueAPI-From-Agent": sender_agent.id},
        )

    # Message create succeeded despite the event emission failure.
    assert resp.status_code == 201
    assert resp.json()["body"] == "should still send"

    # No event recorded (because emit raised).
    events = (
        await db_session.execute(
            select(Event).where(
                Event.recipient_agent_id == recipient_agent.id
            )
        )
    ).scalars().all()
    assert events == []
