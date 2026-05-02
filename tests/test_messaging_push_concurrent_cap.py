"""Per-user concurrent delivery cap tests (Phase 12.1.5 — Slice 4).

Spec: <https://trydock.ai/mike/cueapi-messaging-primitive-v1-sp> §5.6
(concurrent delivery cap).

The messaging primitive shares the ``concurrent:{user_id}`` Redis
counter with cue webhook delivery — same per-user TOTAL cap covers
both. ``settings.MAX_CONCURRENT_DELIVERIES_PER_USER`` (default 50)
gates how many simultaneous outbound deliveries one user can saturate.

Slice 4 wires the cap on both ``deliver_message_task`` and
``retry_message_task``. Differs from the cue-side pattern: when over
cap, the dispatch_outbox row that triggered the worker has already
been marked ``dispatched=true`` by the dispatcher, so we can't just
return and let the poller retry. Instead we insert a fresh outbox
row at ``scheduled_at = now() + 30s`` to recycle through the
dispatcher.

This file pins the contract.
"""
from __future__ import annotations

import datetime as dt
import uuid

import pytest
import redis.asyncio as aioredis
from aiohttp import web
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
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


async def _start_receiver(port: int, status: int = 200):
    received = []

    async def handler(request):
        raw = await request.read()
        received.append({"raw_body": raw, "headers": dict(request.headers)})
        return web.json_response({"ok": True}, status=status)

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
    return user, sender, recipient


def _make_message(user, sender, recipient, body="hello", state="queued") -> Message:
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
        delivery_state=state,
        metadata_={},
        expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30),
    )


def _ctx(db_engine, redis_client):
    return {
        "db_session_factory": async_sessionmaker(
            db_engine, class_=AsyncSession, expire_on_commit=False
        ),
        "redis": redis_client,
    }


# ── tests ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_under_cap_proceeds_normally(db_engine, db_session, redis_client):
    """When concurrent counter is well under the cap, delivery
    proceeds normally and the counter is restored after the call.
    """
    from worker.tasks import deliver_message_task

    secret = generate_webhook_secret()
    received, runner = await _start_receiver(9800, status=200)
    try:
        user, sender, recipient = await _make_user_and_agents(
            db_session,
            recipient_webhook_url="http://localhost:9800/wh",
            recipient_webhook_secret=secret,
        )
        msg = _make_message(user, sender, recipient)
        db_session.add(msg)
        await db_session.commit()

        # Counter starts at 0 (autouse fixture cleans it).
        await deliver_message_task(
            _ctx(db_engine, redis_client),
            {"message_id": msg.id, "to_agent_id": recipient.id},
        )

        # Delivery succeeded.
        assert len(received) == 1
        await db_session.refresh(msg)
        assert msg.delivery_state == "delivered"

        # Counter back to 0.
        val = await redis_client.get(f"concurrent:{user.id}")
        # Either None (key never set) or "0" (set then decremented to 0).
        assert val is None or val == "0"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_over_cap_recycles_message_dispatch(db_engine, db_session, redis_client):
    """When the concurrent counter is at the cap, the worker should:
    - NOT call the recipient's webhook (no HTTP).
    - NOT change message state (still queued).
    - Insert a fresh deliver_message outbox row at scheduled_at = now() + 30s.
    - Decrement back so the counter reflects "we didn't actually start a delivery."
    """
    from worker.tasks import deliver_message_task

    secret = generate_webhook_secret()
    received, runner = await _start_receiver(9801, status=200)
    try:
        user, sender, recipient = await _make_user_and_agents(
            db_session,
            recipient_webhook_url="http://localhost:9801/wh",
            recipient_webhook_secret=secret,
        )
        msg = _make_message(user, sender, recipient)
        db_session.add(msg)
        await db_session.commit()

        # Pre-populate the counter to MAX so the worker's INCR
        # pushes it to MAX+1 → over cap.
        await redis_client.set(
            f"concurrent:{user.id}",
            settings.MAX_CONCURRENT_DELIVERIES_PER_USER,
        )

        before = dt.datetime.now(dt.timezone.utc)
        await deliver_message_task(
            _ctx(db_engine, redis_client),
            {"message_id": msg.id, "to_agent_id": recipient.id},
        )

        # No HTTP call was made.
        assert received == []

        # Message stayed in queued — no claim happened.
        await db_session.refresh(msg)
        assert msg.delivery_state == "queued"
        assert msg.delivering_started_at is None

        # A recycle outbox row was inserted.
        result = await db_session.execute(
            select(DispatchOutbox).where(
                DispatchOutbox.task_type == "deliver_message",
                DispatchOutbox.payload["message_id"].astext == msg.id,
                DispatchOutbox.scheduled_at.isnot(None),
            )
        )
        recycle_rows = result.scalars().all()
        assert len(recycle_rows) == 1
        row = recycle_rows[0]
        # scheduled_at ≈ now + 30s
        delta = (row.scheduled_at - before).total_seconds()
        assert 28 <= delta <= 35, f"expected ~30s, got {delta}"

        # Counter decremented back to MAX (not MAX+1).
        val = await redis_client.get(f"concurrent:{user.id}")
        assert int(val) == settings.MAX_CONCURRENT_DELIVERIES_PER_USER
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_over_cap_recycles_retry_message(db_engine, db_session, redis_client):
    """Same recycle behavior on the retry path. retry_message_task
    over-cap inserts a retry_message outbox row at scheduled_at +30s
    (preserving the attempt count from the original payload).
    """
    from worker.tasks import retry_message_task

    secret = generate_webhook_secret()
    received, runner = await _start_receiver(9802, status=200)
    try:
        user, sender, recipient = await _make_user_and_agents(
            db_session,
            recipient_webhook_url="http://localhost:9802/wh",
            recipient_webhook_secret=secret,
        )
        # Message is in retry_ready (came from a prior attempt).
        msg = _make_message(user, sender, recipient, state="retry_ready")
        db_session.add(msg)
        await db_session.commit()

        # Pre-populate counter to MAX.
        await redis_client.set(
            f"concurrent:{user.id}",
            settings.MAX_CONCURRENT_DELIVERIES_PER_USER,
        )

        await retry_message_task(
            _ctx(db_engine, redis_client),
            {"message_id": msg.id, "to_agent_id": recipient.id, "attempt": 3},
        )

        # No HTTP call.
        assert received == []
        # Message stayed in retry_ready.
        await db_session.refresh(msg)
        assert msg.delivery_state == "retry_ready"

        # Recycle row inserted with task_type = retry_message
        # (NOT deliver_message — recycle preserves task_type so the
        # dispatcher routes back to retry_message_task on next dispatch).
        result = await db_session.execute(
            select(DispatchOutbox).where(
                DispatchOutbox.task_type == "retry_message",
                DispatchOutbox.payload["message_id"].astext == msg.id,
                DispatchOutbox.scheduled_at.isnot(None),
            )
        )
        rows = result.scalars().all()
        assert len(rows) == 1
        # Attempt count preserved through the recycle.
        assert rows[0].payload["attempt"] == 3
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_cap_counter_decremented_on_success(db_engine, db_session, redis_client):
    """After a successful delivery, the concurrent counter MUST be
    decremented so subsequent calls have full cap headroom.
    """
    from worker.tasks import deliver_message_task

    secret = generate_webhook_secret()
    received, runner = await _start_receiver(9803, status=200)
    try:
        user, sender, recipient = await _make_user_and_agents(
            db_session,
            recipient_webhook_url="http://localhost:9803/wh",
            recipient_webhook_secret=secret,
        )
        msg = _make_message(user, sender, recipient)
        db_session.add(msg)
        await db_session.commit()

        # Pre-set counter to 5 to verify exact INCR/DECR balance.
        await redis_client.set(f"concurrent:{user.id}", 5)

        await deliver_message_task(
            _ctx(db_engine, redis_client),
            {"message_id": msg.id, "to_agent_id": recipient.id},
        )

        # Counter back to 5 (worker INCR'd to 6, then DECR'd to 5).
        val = await redis_client.get(f"concurrent:{user.id}")
        assert int(val) == 5
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_cap_counter_decremented_on_failure(db_engine, db_session, redis_client):
    """Even when delivery fails (5xx → retry_ready, 4xx-terminal →
    failed), the concurrent counter must be decremented. The
    decrement happens in a finally block so exceptions don't leak
    counter state.
    """
    from worker.tasks import deliver_message_task

    secret = generate_webhook_secret()
    received, runner = await _start_receiver(9804, status=503)  # retryable failure
    try:
        user, sender, recipient = await _make_user_and_agents(
            db_session,
            recipient_webhook_url="http://localhost:9804/wh",
            recipient_webhook_secret=secret,
        )
        msg = _make_message(user, sender, recipient)
        db_session.add(msg)
        await db_session.commit()

        await redis_client.set(f"concurrent:{user.id}", 3)

        await deliver_message_task(
            _ctx(db_engine, redis_client),
            {"message_id": msg.id, "to_agent_id": recipient.id},
        )

        # Counter back to 3 even though the delivery failed and a
        # retry was scheduled.
        val = await redis_client.get(f"concurrent:{user.id}")
        assert int(val) == 3

        # Sanity: the retry path was taken.
        await db_session.refresh(msg)
        assert msg.delivery_state == "retry_ready"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_redis_blip_does_not_block_delivery(db_engine, db_session):
    """If Redis is unreachable (blip / down), the cap check fails open
    — delivery proceeds. The cap is best-effort, not load-bearing.
    """
    from worker.tasks import deliver_message_task

    secret = generate_webhook_secret()
    received, runner = await _start_receiver(9805, status=200)
    try:
        user, sender, recipient = await _make_user_and_agents(
            db_session,
            recipient_webhook_url="http://localhost:9805/wh",
            recipient_webhook_secret=secret,
        )
        msg = _make_message(user, sender, recipient)
        db_session.add(msg)
        await db_session.commit()

        # Pass redis_client=None to simulate "Redis not configured."
        ctx = {
            "db_session_factory": async_sessionmaker(
                db_engine, class_=AsyncSession, expire_on_commit=False
            ),
        }
        # No "redis" key in ctx → _get_redis returns None → cap check
        # short-circuits to "proceed."
        await deliver_message_task(
            ctx,
            {"message_id": msg.id, "to_agent_id": recipient.id},
        )

        # Delivery still happened.
        assert len(received) == 1
        await db_session.refresh(msg)
        assert msg.delivery_state == "delivered"
    finally:
        await runner.cleanup()
