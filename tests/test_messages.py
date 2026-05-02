"""HTTP-level tests for the Message router (Phase 2.11.3).

Spec: `https://trydock.ai/mike/cueapi-messaging-primitive-v1-sp` §3 (Message primitive) +
§8 (Idempotency-Key) + §10.2 (target test list).

Covers:

* POST /v1/messages create with X-Cueapi-From-Agent + opaque/slug-form
  recipient
* Same-tenant constraint (cross-user → 403)
* Body size cap (32KB), metadata size cap (10KB)
* Thread root invariant (root.thread_id == root.id)
* Reply-in-thread inheritance
* Idempotency-Key dedup + body-mismatch 409
* GET /v1/messages/{id} for sender + recipient + third-party isolation
* /read and /ack state transitions + idempotency
"""
from __future__ import annotations

import uuid

import pytest


async def _make_agent(client, headers, slug=None):
    payload = {"display_name": f"Agent {uuid.uuid4().hex[:6]}", "metadata": {}}
    if slug:
        payload["slug"] = slug
    r = await client.post("/v1/agents", json=payload, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _from_header(agent):
    return {"X-Cueapi-From-Agent": agent["id"]}


@pytest.mark.asyncio
async def test_send_message_minimal(client, auth_headers):
    sender = await _make_agent(client, auth_headers, slug="sender1")
    recipient = await _make_agent(client, auth_headers, slug="recipient1")
    r = await client.post(
        "/v1/messages",
        json={"to": recipient["id"], "body": "hello"},
        headers={**auth_headers, **_from_header(sender)},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"].startswith("msg_")
    assert body["from_agent_id"] == sender["id"]
    assert body["to_agent_id"] == recipient["id"]
    assert body["body"] == "hello"
    assert body["preview"] == "hello"
    assert body["delivery_state"] == "queued"
    assert body["thread_id"] == body["id"]  # root: thread_id == self.id
    assert body["priority"] == 3
    assert body["expects_reply"] is False
    assert body["metadata"] == {}


@pytest.mark.asyncio
async def test_send_message_full_payload(client, auth_headers):
    sender = await _make_agent(client, auth_headers, slug="s2")
    recipient = await _make_agent(client, auth_headers, slug="r2")
    r = await client.post(
        "/v1/messages",
        json={
            "to": recipient["id"],
            "body": "important",
            "subject": "Re: deployment",
            "priority": 5,
            "expects_reply": True,
            "metadata": {"source": "alert"},
        },
        headers={**auth_headers, **_from_header(sender)},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["subject"] == "Re: deployment"
    assert body["priority"] == 5
    assert body["expects_reply"] is True
    assert body["metadata"] == {"source": "alert"}


@pytest.mark.asyncio
async def test_send_message_to_slug_form(client, auth_headers):
    """Slug-form addressing: agent_slug@user_slug per §13 D11."""
    sender = await _make_agent(client, auth_headers, slug="s3")
    await _make_agent(client, auth_headers, slug="my-bot")
    me = await client.get("/v1/auth/me", headers=auth_headers)
    user_slug = me.json()["slug"]

    r = await client.post(
        "/v1/messages",
        json={"to": f"my-bot@{user_slug}", "body": "via slug-form"},
        headers={**auth_headers, **_from_header(sender)},
    )
    assert r.status_code == 201, r.text


@pytest.mark.asyncio
async def test_send_message_missing_from_header_400(client, auth_headers):
    recipient = await _make_agent(client, auth_headers, slug="r4")
    r = await client.post(
        "/v1/messages",
        json={"to": recipient["id"], "body": "hi"},
        headers=auth_headers,
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "missing_from_agent"


@pytest.mark.asyncio
async def test_send_message_unknown_recipient_404(client, auth_headers):
    sender = await _make_agent(client, auth_headers, slug="s5")
    r = await client.post(
        "/v1/messages",
        json={"to": "agt_doesnotexist", "body": "hi"},
        headers={**auth_headers, **_from_header(sender)},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_send_message_cross_tenant_blocked(client, auth_headers, other_auth_headers):
    """v1: same-tenant only. Other user's agent → 403."""
    sender = await _make_agent(client, auth_headers, slug="my-sender")
    other_recipient = await _make_agent(client, other_auth_headers, slug="theirs")
    r = await client.post(
        "/v1/messages",
        json={"to": other_recipient["id"], "body": "hi"},
        headers={**auth_headers, **_from_header(sender)},
    )
    assert r.status_code in (403, 404)
    # Either 403 cross_tenant_messaging_forbidden OR 404 agent_not_found
    # depending on whether resolve_address surfaces the other user's
    # agent. v1 implementation uses the live-agent resolver which DOES
    # find them (any registered agent on the platform), then 403s on
    # tenancy check.
    err = r.json()["error"]["code"]
    assert err in ("cross_tenant_messaging_forbidden", "agent_not_found")


@pytest.mark.asyncio
async def test_send_message_body_too_large_422(client, auth_headers):
    sender = await _make_agent(client, auth_headers, slug="big-s")
    recipient = await _make_agent(client, auth_headers, slug="big-r")
    huge = "a" * 32769
    r = await client.post(
        "/v1/messages",
        json={"to": recipient["id"], "body": huge},
        headers={**auth_headers, **_from_header(sender)},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_send_message_metadata_too_large_400(client, auth_headers):
    sender = await _make_agent(client, auth_headers, slug="big-m-s")
    recipient = await _make_agent(client, auth_headers, slug="big-m-r")
    big_metadata = {"x": "y" * 11_000}  # >10KB JSON
    r = await client.post(
        "/v1/messages",
        json={"to": recipient["id"], "body": "hi", "metadata": big_metadata},
        headers={**auth_headers, **_from_header(sender)},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "metadata_too_large"


@pytest.mark.asyncio
async def test_send_message_from_agent_not_owned_403(client, auth_headers, other_auth_headers):
    """X-Cueapi-From-Agent must be owned by the caller."""
    other_sender = await _make_agent(client, other_auth_headers, slug="other-s")
    recipient = await _make_agent(client, auth_headers, slug="my-r")
    r = await client.post(
        "/v1/messages",
        json={"to": recipient["id"], "body": "hi"},
        headers={**auth_headers, "X-Cueapi-From-Agent": other_sender["id"]},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "from_agent_not_owned"


@pytest.mark.asyncio
async def test_thread_root_self_id(client, auth_headers):
    sender = await _make_agent(client, auth_headers, slug="t1-s")
    recipient = await _make_agent(client, auth_headers, slug="t1-r")
    r = await client.post(
        "/v1/messages",
        json={"to": recipient["id"], "body": "root"},
        headers={**auth_headers, **_from_header(sender)},
    )
    body = r.json()
    assert body["thread_id"] == body["id"]
    assert body["reply_to"] is None


@pytest.mark.asyncio
async def test_reply_inherits_thread_id(client, auth_headers):
    sender = await _make_agent(client, auth_headers, slug="t2-s")
    recipient = await _make_agent(client, auth_headers, slug="t2-r")
    root = await client.post(
        "/v1/messages",
        json={"to": recipient["id"], "body": "root"},
        headers={**auth_headers, **_from_header(sender)},
    )
    root_body = root.json()

    # Recipient replies — flip from/to.
    reply = await client.post(
        "/v1/messages",
        json={"to": sender["id"], "body": "reply!", "reply_to": root_body["id"]},
        headers={**auth_headers, "X-Cueapi-From-Agent": recipient["id"]},
    )
    assert reply.status_code == 201, reply.text
    rb = reply.json()
    assert rb["thread_id"] == root_body["id"]  # inherits root's thread
    assert rb["reply_to"] == root_body["id"]


@pytest.mark.asyncio
async def test_idempotency_key_dedup(client, auth_headers):
    sender = await _make_agent(client, auth_headers, slug="i1-s")
    recipient = await _make_agent(client, auth_headers, slug="i1-r")

    # First call → 201.
    r1 = await client.post(
        "/v1/messages",
        json={"to": recipient["id"], "body": "once"},
        headers={**auth_headers, **_from_header(sender), "Idempotency-Key": "key-A"},
    )
    assert r1.status_code == 201
    msg_id_1 = r1.json()["id"]

    # Same key + same body → 200 (dedup), same id.
    r2 = await client.post(
        "/v1/messages",
        json={"to": recipient["id"], "body": "once"},
        headers={**auth_headers, **_from_header(sender), "Idempotency-Key": "key-A"},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["id"] == msg_id_1


@pytest.mark.asyncio
async def test_idempotency_key_body_mismatch_409(client, auth_headers):
    sender = await _make_agent(client, auth_headers, slug="i2-s")
    recipient = await _make_agent(client, auth_headers, slug="i2-r")

    r1 = await client.post(
        "/v1/messages",
        json={"to": recipient["id"], "body": "first"},
        headers={**auth_headers, **_from_header(sender), "Idempotency-Key": "key-B"},
    )
    assert r1.status_code == 201

    r2 = await client.post(
        "/v1/messages",
        json={"to": recipient["id"], "body": "DIFFERENT"},
        headers={**auth_headers, **_from_header(sender), "Idempotency-Key": "key-B"},
    )
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "idempotency_key_conflict"


@pytest.mark.asyncio
async def test_idempotency_key_per_user_scoped(client, auth_headers, other_auth_headers):
    """Same key under different user_ids → independent (no cross-user
    dedup leak)."""
    s1 = await _make_agent(client, auth_headers, slug="iu-s")
    r1 = await _make_agent(client, auth_headers, slug="iu-r")
    s2 = await _make_agent(client, other_auth_headers, slug="iu-s2")
    r2 = await _make_agent(client, other_auth_headers, slug="iu-r2")

    a = await client.post(
        "/v1/messages",
        json={"to": r1["id"], "body": "one"},
        headers={**auth_headers, **_from_header(s1), "Idempotency-Key": "shared"},
    )
    b = await client.post(
        "/v1/messages",
        json={"to": r2["id"], "body": "two"},
        headers={**other_auth_headers, **_from_header(s2), "Idempotency-Key": "shared"},
    )
    assert a.status_code == 201
    assert b.status_code == 201  # independent, NOT dedup'd


@pytest.mark.asyncio
async def test_get_message_sender_can_read(client, auth_headers):
    sender = await _make_agent(client, auth_headers, slug="g1-s")
    recipient = await _make_agent(client, auth_headers, slug="g1-r")
    sent = await client.post(
        "/v1/messages",
        json={"to": recipient["id"], "body": "hi"},
        headers={**auth_headers, **_from_header(sender)},
    )
    msg_id = sent.json()["id"]
    r = await client.get(f"/v1/messages/{msg_id}", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["id"] == msg_id


@pytest.mark.asyncio
async def test_get_message_third_party_404(client, auth_headers, other_auth_headers):
    """Other user's message is invisible (404, not 403)."""
    sender = await _make_agent(client, auth_headers, slug="g2-s")
    recipient = await _make_agent(client, auth_headers, slug="g2-r")
    sent = await client.post(
        "/v1/messages",
        json={"to": recipient["id"], "body": "hi"},
        headers={**auth_headers, **_from_header(sender)},
    )
    msg_id = sent.json()["id"]
    # Other user can't see it.
    r = await client.get(f"/v1/messages/{msg_id}", headers=other_auth_headers)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_mark_read_idempotent(client, auth_headers):
    sender = await _make_agent(client, auth_headers, slug="rd-s")
    recipient = await _make_agent(client, auth_headers, slug="rd-r")
    sent = await client.post(
        "/v1/messages",
        json={"to": recipient["id"], "body": "read me"},
        headers={**auth_headers, **_from_header(sender)},
    )
    msg_id = sent.json()["id"]

    r1 = await client.post(f"/v1/messages/{msg_id}/read", headers=auth_headers)
    assert r1.status_code == 200
    assert r1.json()["delivery_state"] == "read"
    assert r1.json()["read_at"] is not None

    # Idempotent: second call doesn't error.
    r2 = await client.post(f"/v1/messages/{msg_id}/read", headers=auth_headers)
    assert r2.status_code == 200
    assert r2.json()["delivery_state"] == "read"


@pytest.mark.asyncio
async def test_mark_acked_terminal(client, auth_headers):
    sender = await _make_agent(client, auth_headers, slug="ak-s")
    recipient = await _make_agent(client, auth_headers, slug="ak-r")
    sent = await client.post(
        "/v1/messages",
        json={"to": recipient["id"], "body": "ack me"},
        headers={**auth_headers, **_from_header(sender)},
    )
    msg_id = sent.json()["id"]

    r1 = await client.post(f"/v1/messages/{msg_id}/ack", headers=auth_headers)
    assert r1.status_code == 200
    assert r1.json()["delivery_state"] == "acked"

    # Re-acking → 200 unchanged (idempotent).
    r2 = await client.post(f"/v1/messages/{msg_id}/ack", headers=auth_headers)
    assert r2.status_code == 200

    # Marking as read after ack → 409 (terminal).
    r3 = await client.post(f"/v1/messages/{msg_id}/read", headers=auth_headers)
    assert r3.status_code == 409
    assert r3.json()["error"]["code"] == "invalid_state_transition"
