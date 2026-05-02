"""Push delivery worker tests (Phase 12.1.5 — Slice 2).

Spec: `https://trydock.ai/mike/cueapi-messaging-primitive-v1-sp` §5
(Push delivery), §5.2 (Worker task), §5.3 (Delivery payload + headers).

Slice 2 covers the worker side of the dispatch_outbox path that
Slice 1 enqueued: claim a queued message, POST it to the recipient's
``webhook_url`` with HMAC-signed headers, and transition delivery
state to ``delivered`` on 2xx (or ``failed`` on any non-2xx — Slice
3 will swap that for retry-with-backoff).

Tests in this file:

* Happy path — agent has webhook_url, server returns 2xx, message
  → delivered, headers + body shape match §5.3.
* HMAC signature verifies against the agent's webhook_secret with
  the documented timestamp-binding pattern.
* ``X-CueAPI-Event-Type: message.created`` header is present (Max's
  recipient-side review add — future-proofs for v1.5 state-transition
  webhooks).
* webhook_url cleared between create and delivery time → no-op,
  message stays in ``queued`` for poll-fetchers (per PM design
  decision 2026-04-30).
* Non-2xx response → message → failed (Slice-2 terminal-on-any-error;
  Slice 3 swaps to retry).
* Race: message already past ``queued`` → claim no-ops cleanly.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import uuid

import pytest
from aiohttp import web
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Agent, DispatchOutbox, Message
from app.utils.ids import (
    generate_agent_id,
    generate_api_key,
    generate_message_id,
    generate_webhook_secret,
    get_api_key_prefix,
    hash_api_key,
)


# ── helpers ────────────────────────────────────────────────────────


async def _start_receiver(port: int, status: int = 200, body: dict | None = None):
    """Local HTTP server that records POSTs and returns ``status``."""
    received = []

    async def handler(request):
        # Capture raw body bytes for HMAC verification (recipients
        # MUST use raw bytes, not re-serialized JSON, per Max's
        # review note).
        raw = await request.read()
        received.append(
            {
                "raw_body": raw,
                "body": json.loads(raw),
                "headers": dict(request.headers),
            }
        )
        return web.json_response(body or {"ok": True}, status=status)

    app = web.Application()
    app.router.add_post("/wh", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "localhost", port)
    await site.start()
    return received, runner


async def _make_user_and_agents(
    db_session,
    *,
    recipient_webhook_url: str | None = None,
    recipient_webhook_secret: str | None = None,
):
    """Create a User + sender agent + recipient agent. Returns
    ``(user, sender, recipient, recipient_secret)``.
    """
    from app.models.user import User

    raw = generate_api_key()
    user = User(
        email=f"u-{uuid.uuid4().hex[:8]}@test.com",
        api_key_hash=hash_api_key(raw),
        api_key_prefix=get_api_key_prefix(raw),
        webhook_secret=generate_webhook_secret(),
        slug=f"user-{uuid.uuid4().hex[:8]}",
    )
    db_session.add(user)
    await db_session.flush()

    sender = Agent(
        id=generate_agent_id(),
        user_id=user.id,
        slug=f"sender-{uuid.uuid4().hex[:6]}",
        display_name="Sender",
        metadata_={},
        status="online",
    )
    recipient = Agent(
        id=generate_agent_id(),
        user_id=user.id,
        slug=f"recipient-{uuid.uuid4().hex[:6]}",
        display_name="Recipient",
        webhook_url=recipient_webhook_url,
        webhook_secret=recipient_webhook_secret,
        metadata_={},
        status="online",
    )
    db_session.add(sender)
    db_session.add(recipient)
    await db_session.flush()
    await db_session.commit()
    return user, sender, recipient, recipient_webhook_secret


def _make_message(user, sender, recipient, body="hello") -> Message:
    msg_id = generate_message_id()
    return Message(
        id=msg_id,
        user_id=user.id,
        from_agent_id=sender.id,
        to_agent_id=recipient.id,
        thread_id=msg_id,
        body=body,
        preview=body[:200],
        priority=3,
        delivery_state="queued",
        metadata_={},
        expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30),
    )


# ── tests ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_delivers_and_marks_delivered(db_engine, db_session):
    from worker.tasks import deliver_message_task

    secret = generate_webhook_secret()
    received, runner = await _start_receiver(9989, status=200)
    try:
        user, sender, recipient, _ = await _make_user_and_agents(
            db_session,
            recipient_webhook_url="http://localhost:9989/wh",
            recipient_webhook_secret=secret,
        )
        msg = _make_message(user, sender, recipient, body="ping")
        db_session.add(msg)
        await db_session.commit()

        ctx = {"db_session_factory": async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)}
        await deliver_message_task(ctx, {"message_id": msg.id, "to_agent_id": recipient.id})

        # Receiver got exactly one POST.
        assert len(received) == 1
        body = received[0]["body"]
        # §5.3 body shape
        assert body["id"] == msg.id
        assert body["from"]["agent_id"] == sender.id
        assert body["from"]["slug"] == f"{sender.slug}@{user.slug}"
        assert body["to"]["agent_id"] == recipient.id
        assert body["to"]["slug"] == f"{recipient.slug}@{user.slug}"
        assert body["thread_id"] == msg.id  # root: thread_id == self.id
        assert body["body"] == "ping"
        assert body["priority"] == 3

        # Headers (lowercased, since aiohttp normalizes)
        h = {k.lower(): v for k, v in received[0]["headers"].items()}
        assert h["x-cueapi-message-id"] == msg.id
        assert h["x-cueapi-agent-id"] == recipient.id
        assert h["x-cueapi-thread-id"] == msg.id
        assert h["x-cueapi-attempt"] == "1"
        assert h["x-cueapi-event-type"] == "message.created"  # Max's add
        assert h["x-cueapi-signature"].startswith("v1=")
        assert "x-cueapi-timestamp" in h

        # Message is now delivered.
        await db_session.refresh(msg)
        assert msg.delivery_state == "delivered"
        assert msg.delivered_at is not None
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_signature_verifies_with_per_agent_secret(db_engine, db_session):
    """HMAC verification per the §5.3 verification recipe."""
    from worker.tasks import deliver_message_task

    secret = generate_webhook_secret()
    received, runner = await _start_receiver(9990, status=200)
    try:
        user, sender, recipient, _ = await _make_user_and_agents(
            db_session,
            recipient_webhook_url="http://localhost:9990/wh",
            recipient_webhook_secret=secret,
        )
        msg = _make_message(user, sender, recipient, body="signed")
        db_session.add(msg)
        await db_session.commit()

        ctx = {"db_session_factory": async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)}
        await deliver_message_task(ctx, {"message_id": msg.id, "to_agent_id": recipient.id})

        h = {k.lower(): v for k, v in received[0]["headers"].items()}
        ts = h["x-cueapi-timestamp"]
        sig_header = h["x-cueapi-signature"]
        assert sig_header.startswith("v1=")
        provided_hex = sig_header.split("=", 1)[1]

        # Verification recipe (matches the §5.3 doc): use raw body bytes,
        # NOT re-serialized JSON.
        raw = received[0]["raw_body"]
        signed = f"{ts}.".encode("utf-8") + raw
        expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
        assert hmac.compare_digest(expected, provided_hex)
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_webhook_url_cleared_mid_flight_noops(db_engine, db_session):
    """Recipient cleared webhook_url between message-create and worker
    pick-up → worker no-ops, message STAYS in queued for poll-fetchers.
    """
    from worker.tasks import deliver_message_task

    # Recipient initially has webhook_url + secret; we'll clear them
    # before invoking the worker to simulate mid-flight rotation.
    secret = generate_webhook_secret()
    received, runner = await _start_receiver(9991, status=200)
    try:
        user, sender, recipient, _ = await _make_user_and_agents(
            db_session,
            recipient_webhook_url="http://localhost:9991/wh",
            recipient_webhook_secret=secret,
        )
        msg = _make_message(user, sender, recipient, body="cleared")
        db_session.add(msg)
        await db_session.commit()

        # Clear webhook_url + webhook_secret atomically (paired
        # constraint enforces that they NULL together).
        recipient.webhook_url = None
        recipient.webhook_secret = None
        await db_session.commit()

        ctx = {"db_session_factory": async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)}
        await deliver_message_task(ctx, {"message_id": msg.id, "to_agent_id": recipient.id})

        # No POST was made.
        assert received == []

        # Message stayed in queued (poll-fetchers will pick it up).
        await db_session.refresh(msg)
        assert msg.delivery_state == "queued"
        assert msg.delivered_at is None
        assert msg.failed_at is None
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_terminal_4xx_marks_message_failed(db_engine, db_session):
    """4xx-terminal (404 endpoint_missing) is permanently terminal —
    no retry, even under Slice 3b's classification taxonomy. Verifies
    the non-retryable path through the post-Slice-3b worker.
    """
    from worker.tasks import deliver_message_task

    secret = generate_webhook_secret()
    received, runner = await _start_receiver(9992, status=404, body={"err": "no such route"})
    try:
        user, sender, recipient, _ = await _make_user_and_agents(
            db_session,
            recipient_webhook_url="http://localhost:9992/wh",
            recipient_webhook_secret=secret,
        )
        msg = _make_message(user, sender, recipient, body="will-fail")
        db_session.add(msg)
        await db_session.commit()

        ctx = {"db_session_factory": async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)}
        await deliver_message_task(ctx, {"message_id": msg.id, "to_agent_id": recipient.id})

        assert len(received) == 1  # one attempt, terminal — 404 is not retryable

        await db_session.refresh(msg)
        assert msg.delivery_state == "failed"
        assert msg.failed_at is not None
        assert msg.delivered_at is None
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_claim_noop_when_already_past_queued(db_engine, db_session):
    """If the message has already moved past queued (poll-fetcher won
    the race, or another worker beat us), the conditional UPDATE
    rejects and we return cleanly without POSTing.
    """
    from worker.tasks import deliver_message_task

    secret = generate_webhook_secret()
    received, runner = await _start_receiver(9993, status=200)
    try:
        user, sender, recipient, _ = await _make_user_and_agents(
            db_session,
            recipient_webhook_url="http://localhost:9993/wh",
            recipient_webhook_secret=secret,
        )
        msg = _make_message(user, sender, recipient, body="raced")
        msg.delivery_state = "delivered"  # simulate poll-fetcher won
        msg.delivered_at = dt.datetime.now(dt.timezone.utc)
        db_session.add(msg)
        await db_session.commit()

        ctx = {"db_session_factory": async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)}
        await deliver_message_task(ctx, {"message_id": msg.id, "to_agent_id": recipient.id})

        # No POST.
        assert received == []

        # Message stayed delivered.
        await db_session.refresh(msg)
        assert msg.delivery_state == "delivered"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_to_agent_missing_does_not_explode(db_engine, db_session):
    """If the to_agent was hard-deleted between create and delivery,
    the worker logs and returns rather than raising.
    """
    from worker.tasks import deliver_message_task

    user, sender, recipient, _ = await _make_user_and_agents(
        db_session,
        recipient_webhook_url=None,
        recipient_webhook_secret=None,
    )
    msg = _make_message(user, sender, recipient, body="orphan")
    db_session.add(msg)
    await db_session.commit()

    # Synthesize a payload with a fake to_agent_id that doesn't exist
    # (simulating hard delete; soft-delete would still be findable).
    ctx = {"db_session_factory": async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)}
    await deliver_message_task(ctx, {"message_id": msg.id, "to_agent_id": "agt_nonexistent99"})

    # Message untouched (no claim happened).
    await db_session.refresh(msg)
    assert msg.delivery_state == "queued"
