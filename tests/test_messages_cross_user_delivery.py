"""End-to-end cross-user message delivery tests.

Reproduces and pins the fix for the silent-drop bug surfaced by Dock
(cue.dock.svc on messaging-v1.0.1):

* Symptom: ``POST /v1/messages`` from agent owned by user A to agent
  owned by user B (A ≠ B) returns 201 + persists, but the message
  never appears in B's inbox. Same-user paths work.

* Root cause: pre-fix, both ``list_inbox`` base filter and the atomic
  ``queued → delivered`` UPDATE in ``inbox_service.py`` had a
  ``Message.user_id == user.id`` predicate. ``Message.user_id`` is set
  to the SENDER's user_id at insert time. When the recipient's owner
  polled, the predicate became
  ``WHERE user_id = recipient_owner.id AND ...`` — mathematically
  excluding the cross-user row written with ``user_id = sender_owner.id``.

* Why it didn't surface pre-PR-5b: ``v1`` was implicitly same-tenant
  on the send path, so the sender and recipient always had the same
  ``user_id``. PR-5b's ``WebhookAuthorizationBackend`` started letting
  cross-user sends through but didn't update the data model.

* Fix (Option C): drop ``Message.user_id == user.id`` from the inbox
  read filter and the queued→delivered UPDATE. Inbox visibility is
  gated by AGENT OWNERSHIP, which ``get_agent_owned`` already enforces
  at the route layer. ``Message.user_id`` retains its role as the
  sender/billing scope (idempotency dedup, monthly_message_limit,
  ``list_sent`` filter) — it just doesn't gate inbox reads.

These tests pin both the regression (cross-user delivers now) AND the
existing same-user path (no regression in the common case).
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest

from app.config import settings
from app.services import authorization_backend as authz_module
from app.services.authorization_backend import AuthorizationBackend


# ─── Helpers ──────────────────────────────────────────────────────


async def _make_agent(client, headers, slug=None):
    payload = {"display_name": f"Agent {uuid.uuid4().hex[:6]}", "metadata": {}}
    if slug:
        payload["slug"] = slug
    r = await client.post("/v1/agents", json=payload, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _from_header(agent):
    return {"X-Cueapi-From-Agent": agent["id"]}


class _AlwaysAllowBackend(AuthorizationBackend):
    """Test-only authz backend that allows every cross-user send.

    Mirrors what Dock's WebhookAuthorizationBackend would return when
    the workspace-membership check passes. We use the in-process
    subclass instead of mocking httpx because it's faster + doesn't
    require a network mock.
    """

    async def authorize_message(self, **kwargs) -> bool:
        return True


@contextmanager
def _patch_authz_backend(backend: AuthorizationBackend):
    """Inject a custom authz backend for the duration of a test.

    The backend is module-cached at first call to
    ``get_authorization_backend()``. We poke the cache directly here
    instead of round-tripping through the env-var loader because
    that's both faster and lets us test with a class that's not a
    real Python import path.
    """
    original = authz_module._cached_backend
    authz_module._cached_backend = backend
    try:
        yield
    finally:
        authz_module._cached_backend = original


# ─── 1. Cross-user delivery: the bug ──────────────────────────────


@pytest.mark.asyncio
async def test_cross_user_message_appears_in_recipient_inbox(
    client, auth_headers, other_auth_headers
):
    """Sender (user A) posts a message to recipient agent (owned by
    user B). With AlwaysAllow authz, the send succeeds 201. Recipient's
    inbox poll then surfaces the message — pre-fix this returned an
    empty list because ``Message.user_id == B`` failed to match the
    sender-scoped row.
    """
    sender = await _make_agent(client, auth_headers, slug=f"s-{uuid.uuid4().hex[:6]}")
    recipient = await _make_agent(
        client, other_auth_headers, slug=f"r-{uuid.uuid4().hex[:6]}"
    )

    with _patch_authz_backend(_AlwaysAllowBackend()):
        # Send: A → B's agent
        r = await client.post(
            "/v1/messages",
            json={"to": recipient["id"], "body": "cross-user delivery test"},
            headers={**auth_headers, **_from_header(sender)},
        )
        assert r.status_code == 201, r.text
        msg_id = r.json()["id"]
        # Sender sees their row queued
        assert r.json()["delivery_state"] == "queued"

        # Recipient (user B) polls inbox — pre-fix this returned []
        inbox = await client.get(
            f"/v1/agents/{recipient['id']}/inbox?limit=10",
            headers=other_auth_headers,
        )
        assert inbox.status_code == 200, inbox.text
        msgs = inbox.json()["messages"]
        assert len(msgs) == 1, (
            f"Recipient should see the cross-user message; got {len(msgs)} "
            f"messages. Pre-fix bug: Message.user_id filter excluded it."
        )
        assert msgs[0]["id"] == msg_id
        assert msgs[0]["body"] == "cross-user delivery test"


@pytest.mark.asyncio
async def test_cross_user_message_transitions_queued_to_delivered(
    client, auth_headers, other_auth_headers
):
    """Inbox poll atomically transitions queued → delivered. Pre-fix
    the UPDATE had the same ``Message.user_id == user.id`` predicate
    so the cross-user row stayed queued forever even when the recipient
    polled. Fix removes that predicate; this test pins the new behavior.
    """
    sender = await _make_agent(client, auth_headers, slug=f"s-{uuid.uuid4().hex[:6]}")
    recipient = await _make_agent(
        client, other_auth_headers, slug=f"r-{uuid.uuid4().hex[:6]}"
    )

    with _patch_authz_backend(_AlwaysAllowBackend()):
        r = await client.post(
            "/v1/messages",
            json={"to": recipient["id"], "body": "transition test"},
            headers={**auth_headers, **_from_header(sender)},
        )
        assert r.status_code == 201
        msg_id = r.json()["id"]

        # First poll: triggers queued → delivered.
        inbox = await client.get(
            f"/v1/agents/{recipient['id']}/inbox?limit=10",
            headers=other_auth_headers,
        )
        msgs = inbox.json()["messages"]
        assert len(msgs) == 1
        assert msgs[0]["delivery_state"] == "delivered", (
            f"queued → delivered transition failed for cross-user message. "
            f"Got state: {msgs[0]['delivery_state']}. Pre-fix the UPDATE's "
            f"Message.user_id predicate also matched on sender-only, leaving "
            f"the row stuck in queued."
        )

        # Sender's view also reflects delivered (same row, single column flip).
        sent = await client.get(
            f"/v1/agents/{sender['id']}/sent?limit=10",
            headers={**auth_headers, **_from_header(sender)},
        )
        sent_msgs = sent.json()["messages"]
        assert len(sent_msgs) == 1
        assert sent_msgs[0]["id"] == msg_id
        assert sent_msgs[0]["delivery_state"] == "delivered"


# ─── 2. Same-user regression check ────────────────────────────────


@pytest.mark.asyncio
async def test_same_user_message_unchanged(client, auth_headers):
    """Regression: same-user delivery still works after dropping the
    ``Message.user_id == user.id`` filter from inbox reads. The
    boundary moved to agent-ownership, but for same-user pairs the
    behavior is identical."""
    sender = await _make_agent(client, auth_headers, slug=f"s-{uuid.uuid4().hex[:6]}")
    recipient = await _make_agent(client, auth_headers, slug=f"r-{uuid.uuid4().hex[:6]}")

    r = await client.post(
        "/v1/messages",
        json={"to": recipient["id"], "body": "same-user test"},
        headers={**auth_headers, **_from_header(sender)},
    )
    assert r.status_code == 201
    msg_id = r.json()["id"]

    inbox = await client.get(
        f"/v1/agents/{recipient['id']}/inbox?limit=10",
        headers=auth_headers,
    )
    assert inbox.status_code == 200
    msgs = inbox.json()["messages"]
    assert len(msgs) == 1
    assert msgs[0]["id"] == msg_id
    assert msgs[0]["delivery_state"] == "delivered"


# ─── 3. Inbox isolation: third party can't see the message ────────


@pytest.mark.asyncio
async def test_third_party_cannot_see_cross_user_inbox(
    client, auth_headers, other_auth_headers
):
    """A user who owns NEITHER the from-agent NOR the to-agent must
    not be able to query the recipient's inbox at all. The agent-
    ownership invariant in ``get_agent_owned`` is the boundary; this
    test pins that the bypass-via-Message.user_id-drop didn't open a
    backdoor for unrelated users.

    Setup:
        - User A (auth_headers): owns sender_a + recipient_a
        - User B (other_auth_headers): owns recipient_b
        - A sends cross-user to recipient_b (allowed via authz mock)
        - A third user C (third_party_headers) tries to poll
          recipient_b's inbox — must 403 (not owner of the agent)
    """
    # Spawn a third user inline.
    third_email = f"third-{uuid.uuid4().hex[:8]}@test.com"
    third_resp = await client.post("/v1/auth/register", json={"email": third_email})
    assert third_resp.status_code == 201
    third_party_headers = {
        "Authorization": f"Bearer {third_resp.json()['api_key']}"
    }

    sender = await _make_agent(client, auth_headers, slug=f"s-{uuid.uuid4().hex[:6]}")
    recipient_b = await _make_agent(
        client, other_auth_headers, slug=f"rb-{uuid.uuid4().hex[:6]}"
    )

    with _patch_authz_backend(_AlwaysAllowBackend()):
        r = await client.post(
            "/v1/messages",
            json={"to": recipient_b["id"], "body": "isolation test"},
            headers={**auth_headers, **_from_header(sender)},
        )
        assert r.status_code == 201

        # Third party tries to poll recipient_b's inbox using the
        # opaque agent_id — must 404 (not their agent).
        leak_attempt = await client.get(
            f"/v1/agents/{recipient_b['id']}/inbox?limit=10",
            headers=third_party_headers,
        )
        # 404 (agent not visible to caller) is the right shape; never
        # 200-with-message-leaked.
        assert leak_attempt.status_code == 404, (
            f"Third-party access to recipient_b's inbox must be denied. "
            f"Got {leak_attempt.status_code}. Inbox visibility moved to "
            f"agent ownership; that boundary must hold."
        )


# ─── 4. GET /messages/{id} cross-user access ──────────────────────


@pytest.mark.asyncio
async def test_get_message_by_id_recipient_can_read_cross_user(
    client, auth_headers, other_auth_headers
):
    """GET /v1/messages/{id} must accept BOTH the sender AND the
    recipient's owner for cross-user messages. Pre-fix the predicate
    was ``str(msg.user_id) != str(user.id)`` which only allowed the
    sender. Fix: also allow the owner of ``to_agent``.
    """
    sender = await _make_agent(client, auth_headers, slug=f"s-{uuid.uuid4().hex[:6]}")
    recipient = await _make_agent(
        client, other_auth_headers, slug=f"r-{uuid.uuid4().hex[:6]}"
    )

    with _patch_authz_backend(_AlwaysAllowBackend()):
        r = await client.post(
            "/v1/messages",
            json={"to": recipient["id"], "body": "GET test"},
            headers={**auth_headers, **_from_header(sender)},
        )
        assert r.status_code == 201
        msg_id = r.json()["id"]

        # Sender (A) GETs their own message — allowed.
        sender_get = await client.get(
            f"/v1/messages/{msg_id}", headers=auth_headers
        )
        assert sender_get.status_code == 200, sender_get.text
        assert sender_get.json()["id"] == msg_id

        # Recipient owner (B) GETs the message — must also be allowed.
        recipient_get = await client.get(
            f"/v1/messages/{msg_id}", headers=other_auth_headers
        )
        assert recipient_get.status_code == 200, (
            f"Recipient owner must be able to GET the cross-user message. "
            f"Got {recipient_get.status_code}: {recipient_get.text}"
        )
        assert recipient_get.json()["id"] == msg_id


@pytest.mark.asyncio
async def test_get_message_by_id_third_party_gets_404(
    client, auth_headers, other_auth_headers
):
    """A user who is neither sender nor recipient gets 404 (not 403,
    so existence doesn't leak). Pins the isolation invariant after
    the GET-message visibility expansion."""
    third_email = f"third-{uuid.uuid4().hex[:8]}@test.com"
    third_resp = await client.post("/v1/auth/register", json={"email": third_email})
    third_party_headers = {
        "Authorization": f"Bearer {third_resp.json()['api_key']}"
    }

    sender = await _make_agent(client, auth_headers, slug=f"s-{uuid.uuid4().hex[:6]}")
    recipient = await _make_agent(
        client, other_auth_headers, slug=f"r-{uuid.uuid4().hex[:6]}"
    )

    with _patch_authz_backend(_AlwaysAllowBackend()):
        r = await client.post(
            "/v1/messages",
            json={"to": recipient["id"], "body": "third-party isolation"},
            headers={**auth_headers, **_from_header(sender)},
        )
        msg_id = r.json()["id"]

        third_get = await client.get(
            f"/v1/messages/{msg_id}", headers=third_party_headers
        )
        assert third_get.status_code == 404, (
            f"Third party must get 404 (not 403, not 200). "
            f"Got {third_get.status_code}."
        )
