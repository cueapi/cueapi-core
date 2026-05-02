"""Push-delivery enqueue tests (Phase 12.1.5 — Slice 1).

Spec: `https://trydock.ai/mike/cueapi-messaging-primitive-v1-sp` §5.1 (Push delivery — Dispatch
trigger).

When a message is created and the recipient agent has a
``webhook_url`` configured, ``create_message`` inserts a
``dispatch_outbox`` row with ``task_type='deliver_message'`` in the
SAME transaction as the message row. The worker that actually
performs the HTTP POST + claim + retry lands in subsequent slices.
This file pins the enqueue contract.

What's tested here:

* webhook_url set on recipient → outbox row created with correct payload
* webhook_url NOT set → no outbox row created (poll-only path)
* Idempotency-Key dedup hit → no SECOND outbox row (returns existing message)
* Outbox row carries NULL ``execution_id``/``cue_id`` (message-task
  shape; enforced by ``task_payload_shape`` check constraint)
* ``webhook_secret`` is NOT snapshotted in the payload (re-read live by
  worker so rotation takes effect immediately)

What's NOT tested here (lands in subsequent slices):

* Worker claim + HTTP POST + retry: Slice 2.
* SSRF re-validation at delivery time: Slice 2/4.
* State-transition webhooks (``message.delivered`` etc.): v1.5b.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.models import DispatchOutbox


async def _make_agent(client, headers, slug=None, webhook_url=None):
    payload = {"display_name": f"Agent {uuid.uuid4().hex[:6]}", "metadata": {}}
    if slug:
        payload["slug"] = slug
    if webhook_url:
        payload["webhook_url"] = webhook_url
    r = await client.post("/v1/agents", json=payload, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _from_header(agent):
    return {"X-Cueapi-From-Agent": agent["id"]}


async def _outbox_rows_for_message(db_session, msg_id: str):
    result = await db_session.execute(
        select(DispatchOutbox).where(
            DispatchOutbox.task_type == "deliver_message",
            DispatchOutbox.payload["message_id"].astext == msg_id,
        )
    )
    return result.scalars().all()


@pytest.mark.asyncio
async def test_outbox_row_created_when_recipient_has_webhook_url(
    client, auth_headers, db_session
):
    sender = await _make_agent(client, auth_headers, slug="push-sender")
    recipient = await _make_agent(
        client,
        auth_headers,
        slug="push-recipient",
        webhook_url="https://example.com/wh/inbound",
    )
    r = await client.post(
        "/v1/messages",
        json={"to": recipient["id"], "body": "deliver me"},
        headers={**auth_headers, **_from_header(sender)},
    )
    assert r.status_code == 201, r.text
    msg_id = r.json()["id"]

    rows = await _outbox_rows_for_message(db_session, msg_id)
    assert len(rows) == 1, (
        f"expected one deliver_message outbox row for {msg_id}, "
        f"got {len(rows)}"
    )
    row = rows[0]
    assert row.task_type == "deliver_message"
    assert row.execution_id is None, "message-task rows must have NULL execution_id"
    assert row.cue_id is None, "message-task rows must have NULL cue_id"
    assert row.dispatched is False
    # Payload contract per §5.1.
    assert row.payload["message_id"] == msg_id
    assert row.payload["to_agent_id"] == recipient["id"]
    assert row.payload["webhook_url"] == "https://example.com/wh/inbound"
    # webhook_secret intentionally NOT snapshotted — worker re-reads live.
    assert "webhook_secret" not in row.payload


@pytest.mark.asyncio
async def test_no_outbox_row_when_recipient_is_poll_only(
    client, auth_headers, db_session
):
    sender = await _make_agent(client, auth_headers, slug="poll-sender")
    recipient = await _make_agent(client, auth_headers, slug="poll-recipient")
    # Recipient has NO webhook_url — poll-only path.
    assert recipient["webhook_url"] is None

    r = await client.post(
        "/v1/messages",
        json={"to": recipient["id"], "body": "fetch me"},
        headers={**auth_headers, **_from_header(sender)},
    )
    assert r.status_code == 201, r.text
    msg_id = r.json()["id"]

    rows = await _outbox_rows_for_message(db_session, msg_id)
    assert rows == [], (
        f"expected no outbox rows for poll-only recipient; got {len(rows)}"
    )


@pytest.mark.asyncio
async def test_idempotency_dedup_does_not_double_enqueue(
    client, auth_headers, db_session
):
    """Replaying with the same Idempotency-Key returns the existing
    message and MUST NOT insert a second outbox row.
    """
    sender = await _make_agent(client, auth_headers, slug="idem-sender")
    recipient = await _make_agent(
        client,
        auth_headers,
        slug="idem-recipient",
        webhook_url="https://example.com/wh/idem",
    )
    headers = {
        **auth_headers,
        **_from_header(sender),
        "Idempotency-Key": "slice1-test-key",
    }
    body = {"to": recipient["id"], "body": "deliver once"}

    r1 = await client.post("/v1/messages", json=body, headers=headers)
    assert r1.status_code == 201, r1.text
    msg_id = r1.json()["id"]

    r2 = await client.post("/v1/messages", json=body, headers=headers)
    # Dedup-hit returns 200 with the same message id (per §8.2 strict mode).
    assert r2.status_code == 200, r2.text
    assert r2.json()["id"] == msg_id

    rows = await _outbox_rows_for_message(db_session, msg_id)
    assert len(rows) == 1, (
        f"idempotency replay must not double-enqueue; "
        f"expected 1 outbox row, got {len(rows)}"
    )
