"""Tests for §13 (Phase 12.1.7): per-message scheduling on POST /v1/messages.

Optional ``send_at`` timestamp on MessageCreate delays delivery until
the time elapses. Same shape as cue-fire send_at, ported to the
messaging primitive. Ports cueapi/cueapi#623.

These tests pin:

1. ``send_at`` omitted → existing behavior (immediate delivery,
   inbox shows the message).
2. ``send_at`` in the future → message persisted but recipient's
   inbox query gates it out until the time passes.
3. ``send_at`` in the future → DispatchOutbox.scheduled_at set so push
   delivery is also gated.
4. ``send_at`` in the past → forgiving fallback to "send now" (no error).
5. Recipient's inbox queued→delivered transition skips
   scheduled-but-not-yet-due messages; surfaces them after the time
   passes and transitions them on first post-due poll.
6. Sender's `sent` view DOES show scheduled messages (they should see
   what they queued).
7. Invalid timestamps return 422.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.dispatch_outbox import DispatchOutbox
from app.models.message import Message


async def _create_agent(client, auth_headers, slug, *, webhook_url=None):
    body = {"slug": slug, "display_name": slug.title()}
    if webhook_url:
        body["webhook_url"] = webhook_url
    resp = await client.post("/v1/agents", json=body, headers=auth_headers)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


async def _send(client, auth_headers, from_agent_id, body):
    return await client.post(
        "/v1/messages",
        json=body,
        headers={**auth_headers, "X-Cueapi-From-Agent": from_agent_id},
    )


@pytest.mark.asyncio
async def test_send_at_omitted_immediate_delivery(client, auth_headers):
    sender = await _create_agent(client, auth_headers, "sa-sender-1")
    rcpt = await _create_agent(client, auth_headers, "sa-rcpt-1")
    resp = await _send(client, auth_headers, sender["id"], {"to": rcpt["id"], "body": "hi"})
    assert resp.status_code == 201
    assert resp.json()["send_at"] is None

    inbox = await client.get(f"/v1/agents/{rcpt['id']}/inbox", headers=auth_headers)
    assert inbox.status_code == 200
    msg_ids = [m["id"] for m in inbox.json()["messages"]]
    assert resp.json()["id"] in msg_ids


@pytest.mark.asyncio
async def test_send_at_future_invisible_in_inbox(client, auth_headers, db_session):
    sender = await _create_agent(client, auth_headers, "sa-sender-2")
    rcpt = await _create_agent(client, auth_headers, "sa-rcpt-2")
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    resp = await _send(
        client, auth_headers, sender["id"],
        {"to": rcpt["id"], "body": "future-msg", "send_at": future.isoformat()},
    )
    assert resp.status_code == 201
    msg_id = resp.json()["id"]
    parsed = datetime.fromisoformat(resp.json()["send_at"])
    assert abs((parsed - future).total_seconds()) < 1.0

    msg = (await db_session.execute(select(Message).where(Message.id == msg_id))).scalar_one()
    assert msg.send_at is not None
    assert abs((msg.send_at - future).total_seconds()) < 1.0
    assert msg.delivery_state == "queued"

    inbox = await client.get(f"/v1/agents/{rcpt['id']}/inbox", headers=auth_headers)
    assert inbox.status_code == 200
    msg_ids = [m["id"] for m in inbox.json()["messages"]]
    assert msg_id not in msg_ids


@pytest.mark.asyncio
async def test_send_at_future_outbox_scheduled_at_set(client, auth_headers, db_session):
    """When recipient has a webhook, the outbox row's scheduled_at is
    set so the dispatcher gates push delivery until send_at."""
    sender = await _create_agent(client, auth_headers, "sa-sender-3")
    rcpt = await _create_agent(
        client, auth_headers, "sa-rcpt-3", webhook_url="https://example.com/wh"
    )
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    resp = await _send(
        client, auth_headers, sender["id"],
        {"to": rcpt["id"], "body": "scheduled push", "send_at": future.isoformat()},
    )
    assert resp.status_code == 201
    msg_id = resp.json()["id"]

    outbox = (
        await db_session.execute(
            select(DispatchOutbox).where(
                DispatchOutbox.task_type == "deliver_message",
                DispatchOutbox.payload["message_id"].astext == msg_id,
            )
        )
    ).scalar_one()
    assert outbox.scheduled_at is not None
    assert abs((outbox.scheduled_at - future).total_seconds()) < 1.0


@pytest.mark.asyncio
async def test_send_at_past_falls_back_to_now(client, auth_headers, db_session):
    sender = await _create_agent(client, auth_headers, "sa-sender-4")
    rcpt = await _create_agent(client, auth_headers, "sa-rcpt-4")
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    resp = await _send(
        client, auth_headers, sender["id"],
        {"to": rcpt["id"], "body": "past-msg", "send_at": past.isoformat()},
    )
    assert resp.status_code == 201
    msg_id = resp.json()["id"]

    msg = (await db_session.execute(select(Message).where(Message.id == msg_id))).scalar_one()
    assert msg.send_at is None
    inbox = await client.get(f"/v1/agents/{rcpt['id']}/inbox", headers=auth_headers)
    msg_ids = [m["id"] for m in inbox.json()["messages"]]
    assert msg_id in msg_ids


@pytest.mark.asyncio
async def test_send_at_future_visible_after_send_at_passes(client, auth_headers, db_session):
    """When a scheduled message's send_at falls into the past, the
    next inbox poll surfaces it AND atomically transitions it to delivered."""
    sender = await _create_agent(client, auth_headers, "sa-sender-5")
    rcpt = await _create_agent(client, auth_headers, "sa-rcpt-5")
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    resp = await _send(
        client, auth_headers, sender["id"],
        {"to": rcpt["id"], "body": "future-msg-5", "send_at": future.isoformat()},
    )
    msg_id = resp.json()["id"]

    msg = (await db_session.execute(select(Message).where(Message.id == msg_id))).scalar_one()
    msg.send_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    await db_session.commit()

    inbox = await client.get(f"/v1/agents/{rcpt['id']}/inbox", headers=auth_headers)
    msg_ids = [m["id"] for m in inbox.json()["messages"]]
    assert msg_id in msg_ids

    db_session.expire_all()
    msg2 = (await db_session.execute(select(Message).where(Message.id == msg_id))).scalar_one()
    assert msg2.delivery_state == "delivered"


@pytest.mark.asyncio
async def test_send_at_future_visible_in_sender_sent_view(client, auth_headers):
    """Sender's `sent` view shows scheduled messages (they queued them
    deliberately; should see them)."""
    sender = await _create_agent(client, auth_headers, "sa-sender-6")
    rcpt = await _create_agent(client, auth_headers, "sa-rcpt-6")
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    resp = await _send(
        client, auth_headers, sender["id"],
        {"to": rcpt["id"], "body": "scheduled", "send_at": future.isoformat()},
    )
    msg_id = resp.json()["id"]

    sent = await client.get(f"/v1/agents/{sender['id']}/sent", headers=auth_headers)
    assert sent.status_code == 200
    msg_ids = [m["id"] for m in sent.json()["messages"]]
    assert msg_id in msg_ids


@pytest.mark.asyncio
async def test_send_at_invalid_timestamp_returns_422(client, auth_headers):
    sender = await _create_agent(client, auth_headers, "sa-sender-7")
    rcpt = await _create_agent(client, auth_headers, "sa-rcpt-7")
    resp = await _send(
        client, auth_headers, sender["id"],
        {"to": rcpt["id"], "body": "bad", "send_at": "not-a-date"},
    )
    assert resp.status_code in (400, 422)
