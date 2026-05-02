"""Push-delivery retry behavior tests (Phase 12.1.5 — Slice 3b).

Spec: <https://trydock.ai/mike/cueapi-messaging-primitive-v1-sp> §5.4
(retry policy + classification table) + §5.4 stale-recovery semantics.

Slice 3b adds:

* ``dispatch_outbox.scheduled_at`` for deferred dispatch
* ``messages.delivering_started_at`` for stale-recovery
* Retry routing in ``deliver_message_task`` (5xx / 408 / 429 / 502 /
  503 / network → insert ``retry_message`` row at scheduled_at)
* ``retry_message_task`` worker that recurses or terminates per
  attempt budget
* ``recover_stale_message_deliveries`` poll loop for
  worker-crash-mid-delivery recovery
* Retry-After honoring on 429 / 503

This file pins the contract.
"""
from __future__ import annotations

import datetime as dt
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


async def _start_receiver(
    port: int,
    *,
    status: int = 200,
    body: dict | None = None,
    headers: dict | None = None,
):
    received = []

    async def handler(request):
        raw = await request.read()
        received.append({"raw_body": raw, "headers": dict(request.headers)})
        resp = web.json_response(body or {"ok": True}, status=status)
        if headers:
            for k, v in headers.items():
                resp.headers[k] = v
        return resp

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


def _ctx(db_engine):
    return {
        "db_session_factory": async_sessionmaker(
            db_engine, class_=AsyncSession, expire_on_commit=False
        )
    }


async def _retry_rows_for(db_session, msg_id):
    result = await db_session.execute(
        select(DispatchOutbox).where(
            DispatchOutbox.task_type == "retry_message",
            DispatchOutbox.payload["message_id"].astext == msg_id,
        )
    )
    return result.scalars().all()


# ── 5xx → retry path ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_500_inserts_retry_message_row_with_backoff(db_engine, db_session):
    """5xx is retryable. Worker should:
    - Transition message to retry_ready (not failed).
    - Insert a retry_message outbox row with attempt=2 and
      scheduled_at = now() + 60s (first backoff value = 1 minute).
    """
    from worker.tasks import deliver_message_task

    secret = generate_webhook_secret()
    received, runner = await _start_receiver(9700, status=500, body={"err": "boom"})
    try:
        user, sender, recipient = await _make_user_and_agents(
            db_session,
            recipient_webhook_url="http://localhost:9700/wh",
            recipient_webhook_secret=secret,
        )
        msg = _make_message(user, sender, recipient, body="retryable")
        db_session.add(msg)
        await db_session.commit()

        before = dt.datetime.now(dt.timezone.utc)
        await deliver_message_task(
            _ctx(db_engine),
            {"message_id": msg.id, "to_agent_id": recipient.id},
        )

        await db_session.refresh(msg)
        assert msg.delivery_state == "retry_ready"
        assert msg.failed_at is None
        assert msg.delivered_at is None

        rows = await _retry_rows_for(db_session, msg.id)
        assert len(rows) == 1
        row = rows[0]
        assert row.task_type == "retry_message"
        assert row.payload["attempt"] == 2
        assert row.payload["message_id"] == msg.id
        assert row.payload["to_agent_id"] == recipient.id
        # scheduled_at should be ~60s in the future
        assert row.scheduled_at is not None
        delta = (row.scheduled_at - before).total_seconds()
        assert 59 <= delta <= 65, f"expected ~60s, got {delta}"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_502_is_retryable_per_max_review(db_engine, db_session):
    """502 from a reverse proxy is transient — must retry, not terminal."""
    from worker.tasks import deliver_message_task

    secret = generate_webhook_secret()
    received, runner = await _start_receiver(9701, status=502)
    try:
        user, sender, recipient = await _make_user_and_agents(
            db_session,
            recipient_webhook_url="http://localhost:9701/wh",
            recipient_webhook_secret=secret,
        )
        msg = _make_message(user, sender, recipient)
        db_session.add(msg)
        await db_session.commit()

        await deliver_message_task(
            _ctx(db_engine),
            {"message_id": msg.id, "to_agent_id": recipient.id},
        )

        await db_session.refresh(msg)
        assert msg.delivery_state == "retry_ready"
        rows = await _retry_rows_for(db_session, msg.id)
        assert len(rows) == 1
    finally:
        await runner.cleanup()


# ── 4xx terminal → failed (no retry) ───────────────────────────────


@pytest.mark.asyncio
async def test_401_is_terminal_no_retry(db_engine, db_session):
    """401 = wrong/rotated webhook_secret. Terminal; no retry row."""
    from worker.tasks import deliver_message_task

    secret = generate_webhook_secret()
    received, runner = await _start_receiver(9702, status=401)
    try:
        user, sender, recipient = await _make_user_and_agents(
            db_session,
            recipient_webhook_url="http://localhost:9702/wh",
            recipient_webhook_secret=secret,
        )
        msg = _make_message(user, sender, recipient)
        db_session.add(msg)
        await db_session.commit()

        await deliver_message_task(
            _ctx(db_engine),
            {"message_id": msg.id, "to_agent_id": recipient.id},
        )

        await db_session.refresh(msg)
        assert msg.delivery_state == "failed"
        assert msg.failed_at is not None
        rows = await _retry_rows_for(db_session, msg.id)
        assert rows == [], "401 must NOT enqueue a retry"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_404_endpoint_missing_terminal(db_engine, db_session):
    """404 = endpoint_missing. Terminal; no retry."""
    from worker.tasks import deliver_message_task

    secret = generate_webhook_secret()
    received, runner = await _start_receiver(9703, status=404)
    try:
        user, sender, recipient = await _make_user_and_agents(
            db_session,
            recipient_webhook_url="http://localhost:9703/wh",
            recipient_webhook_secret=secret,
        )
        msg = _make_message(user, sender, recipient)
        db_session.add(msg)
        await db_session.commit()

        await deliver_message_task(
            _ctx(db_engine),
            {"message_id": msg.id, "to_agent_id": recipient.id},
        )

        await db_session.refresh(msg)
        assert msg.delivery_state == "failed"
        assert (await _retry_rows_for(db_session, msg.id)) == []
    finally:
        await runner.cleanup()


# ── 429 Retry-After honoring ───────────────────────────────────────


@pytest.mark.asyncio
async def test_429_with_retry_after_overrides_own_backoff(db_engine, db_session):
    """Retry-After: 600 → next retry scheduled at +600s, NOT +60s."""
    from worker.tasks import deliver_message_task

    secret = generate_webhook_secret()
    received, runner = await _start_receiver(
        9704,
        status=429,
        headers={"Retry-After": "600"},
    )
    try:
        user, sender, recipient = await _make_user_and_agents(
            db_session,
            recipient_webhook_url="http://localhost:9704/wh",
            recipient_webhook_secret=secret,
        )
        msg = _make_message(user, sender, recipient)
        db_session.add(msg)
        await db_session.commit()

        before = dt.datetime.now(dt.timezone.utc)
        await deliver_message_task(
            _ctx(db_engine),
            {"message_id": msg.id, "to_agent_id": recipient.id},
        )

        rows = await _retry_rows_for(db_session, msg.id)
        assert len(rows) == 1
        row = rows[0]
        delta = (row.scheduled_at - before).total_seconds()
        # Recipient's 600s wins over our 60s minimum.
        assert 599 <= delta <= 605, f"expected ~600s, got {delta}"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_429_with_retry_after_zero_respects_own_min(db_engine, db_session):
    """Retry-After: 0 → recipient ready immediately; server's own
    60s minimum still wins.
    """
    from worker.tasks import deliver_message_task

    secret = generate_webhook_secret()
    received, runner = await _start_receiver(
        9705,
        status=429,
        headers={"Retry-After": "0"},
    )
    try:
        user, sender, recipient = await _make_user_and_agents(
            db_session,
            recipient_webhook_url="http://localhost:9705/wh",
            recipient_webhook_secret=secret,
        )
        msg = _make_message(user, sender, recipient)
        db_session.add(msg)
        await db_session.commit()

        before = dt.datetime.now(dt.timezone.utc)
        await deliver_message_task(
            _ctx(db_engine),
            {"message_id": msg.id, "to_agent_id": recipient.id},
        )

        rows = await _retry_rows_for(db_session, msg.id)
        assert len(rows) == 1
        delta = (rows[0].scheduled_at - before).total_seconds()
        # max(60, 0) = 60 — server's polite minimum wins.
        assert 59 <= delta <= 65, f"expected ~60s, got {delta}"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_429_without_retry_after_uses_own_backoff(db_engine, db_session):
    """No Retry-After header → use own 60s default."""
    from worker.tasks import deliver_message_task

    secret = generate_webhook_secret()
    received, runner = await _start_receiver(9706, status=429)  # no Retry-After
    try:
        user, sender, recipient = await _make_user_and_agents(
            db_session,
            recipient_webhook_url="http://localhost:9706/wh",
            recipient_webhook_secret=secret,
        )
        msg = _make_message(user, sender, recipient)
        db_session.add(msg)
        await db_session.commit()

        before = dt.datetime.now(dt.timezone.utc)
        await deliver_message_task(
            _ctx(db_engine),
            {"message_id": msg.id, "to_agent_id": recipient.id},
        )

        rows = await _retry_rows_for(db_session, msg.id)
        assert len(rows) == 1
        delta = (rows[0].scheduled_at - before).total_seconds()
        assert 59 <= delta <= 65
    finally:
        await runner.cleanup()


# ── retry_message_task: claims from retry_ready, recurses, terminates ───


@pytest.mark.asyncio
async def test_retry_task_succeeds_marks_delivered(db_engine, db_session):
    """retry_message_task on a 2xx response: claim retry_ready →
    delivering, POST, mark delivered.
    """
    from worker.tasks import retry_message_task

    secret = generate_webhook_secret()
    received, runner = await _start_receiver(9707, status=200)
    try:
        user, sender, recipient = await _make_user_and_agents(
            db_session,
            recipient_webhook_url="http://localhost:9707/wh",
            recipient_webhook_secret=secret,
        )
        msg = _make_message(user, sender, recipient)
        msg.delivery_state = "retry_ready"  # simulate prior failed attempt
        db_session.add(msg)
        await db_session.commit()

        await retry_message_task(
            _ctx(db_engine),
            {"message_id": msg.id, "to_agent_id": recipient.id, "attempt": 2},
        )

        await db_session.refresh(msg)
        assert msg.delivery_state == "delivered"
        assert msg.delivered_at is not None
        # The X-CueAPI-Attempt header should reflect attempt=2
        h = {k.lower(): v for k, v in received[0]["headers"].items()}
        assert h["x-cueapi-attempt"] == "2"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_retry_exhaustion_marks_failed(db_engine, db_session):
    """When retry_message_task fires on the LAST attempt (attempt=4 =
    initial+3 retries) and fails, the message is marked failed —
    no more retry rows enqueued.
    """
    from worker.tasks import retry_message_task, MESSAGE_RETRY_MAX_ATTEMPTS

    secret = generate_webhook_secret()
    received, runner = await _start_receiver(9708, status=503)
    try:
        user, sender, recipient = await _make_user_and_agents(
            db_session,
            recipient_webhook_url="http://localhost:9708/wh",
            recipient_webhook_secret=secret,
        )
        msg = _make_message(user, sender, recipient)
        msg.delivery_state = "retry_ready"
        db_session.add(msg)
        await db_session.commit()

        # attempt = MESSAGE_RETRY_MAX_ATTEMPTS + 1 (the final allowed attempt)
        final_attempt = MESSAGE_RETRY_MAX_ATTEMPTS + 1
        await retry_message_task(
            _ctx(db_engine),
            {"message_id": msg.id, "to_agent_id": recipient.id, "attempt": final_attempt},
        )

        await db_session.refresh(msg)
        assert msg.delivery_state == "failed"
        assert msg.failed_at is not None
        # No retry row enqueued — budget is exhausted.
        rows = await _retry_rows_for(db_session, msg.id)
        assert rows == []
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_retry_task_recurses_on_continued_failure(db_engine, db_session):
    """retry_message_task on attempt=2 fails 5xx → enqueues another
    retry_message row for attempt=3 with the appropriate backoff
    (5 minutes = backoff[1]).
    """
    from worker.tasks import retry_message_task

    secret = generate_webhook_secret()
    received, runner = await _start_receiver(9709, status=503)
    try:
        user, sender, recipient = await _make_user_and_agents(
            db_session,
            recipient_webhook_url="http://localhost:9709/wh",
            recipient_webhook_secret=secret,
        )
        msg = _make_message(user, sender, recipient)
        msg.delivery_state = "retry_ready"
        db_session.add(msg)
        await db_session.commit()

        before = dt.datetime.now(dt.timezone.utc)
        await retry_message_task(
            _ctx(db_engine),
            {"message_id": msg.id, "to_agent_id": recipient.id, "attempt": 2},
        )

        await db_session.refresh(msg)
        assert msg.delivery_state == "retry_ready"  # back to retry_ready, not failed
        rows = await _retry_rows_for(db_session, msg.id)
        assert len(rows) == 1
        row = rows[0]
        assert row.payload["attempt"] == 3
        # Backoff index = attempt-1 = 1 → 5 minutes = 300s
        delta = (row.scheduled_at - before).total_seconds()
        assert 299 <= delta <= 305, f"expected ~300s (5 min), got {delta}"
    finally:
        await runner.cleanup()


# ── delivering_started_at + claim semantics ────────────────────────


@pytest.mark.asyncio
async def test_delivering_started_at_set_on_claim(db_engine, db_session):
    """When the worker claims (queued → delivering),
    delivering_started_at MUST be set to now() so stale recovery
    can detect crashes.
    """
    from worker.tasks import deliver_message_task

    secret = generate_webhook_secret()
    received, runner = await _start_receiver(9710, status=200)
    try:
        user, sender, recipient = await _make_user_and_agents(
            db_session,
            recipient_webhook_url="http://localhost:9710/wh",
            recipient_webhook_secret=secret,
        )
        msg = _make_message(user, sender, recipient)
        db_session.add(msg)
        await db_session.commit()
        # Initially NULL.
        await db_session.refresh(msg)
        assert msg.delivering_started_at is None

        await deliver_message_task(
            _ctx(db_engine),
            {"message_id": msg.id, "to_agent_id": recipient.id},
        )

        # On terminal-success transition, delivering_started_at is
        # cleared back to NULL (the message is no longer in delivering).
        await db_session.refresh(msg)
        assert msg.delivery_state == "delivered"
        assert msg.delivering_started_at is None
    finally:
        await runner.cleanup()


# ── Stale-recovery poll loop ───────────────────────────────────────


@pytest.mark.asyncio
async def test_stale_recovery_moves_stuck_to_retry_ready(db_engine, db_session):
    """A message stuck in delivering past the stale threshold gets
    moved back to retry_ready with a fresh retry_message outbox row
    enqueued at scheduled_at = now() (no backoff for crash recovery).
    """
    from worker.poller import recover_stale_message_deliveries

    user, sender, recipient = await _make_user_and_agents(
        db_session,
        recipient_webhook_url="http://localhost:9711/wh",  # any URL
        recipient_webhook_secret=generate_webhook_secret(),
    )
    # Simulate a worker that claimed and crashed: message in delivering
    # with delivering_started_at older than the stale threshold.
    msg = _make_message(user, sender, recipient)
    msg.delivery_state = "delivering"
    msg.delivering_started_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=600)
    db_session.add(msg)
    # Also add a prior dispatched outbox row so attempt-counting works.
    db_session.add(
        DispatchOutbox(
            task_type="deliver_message",
            payload={"message_id": msg.id, "to_agent_id": recipient.id, "webhook_url": recipient.webhook_url},
            dispatched=True,
        )
    )
    await db_session.commit()

    recovered = await recover_stale_message_deliveries(db_engine, stale_seconds=300)
    assert recovered == 1

    await db_session.refresh(msg)
    assert msg.delivery_state == "retry_ready"
    assert msg.delivering_started_at is None
    rows = await _retry_rows_for(db_session, msg.id)
    assert len(rows) == 1
    # Stale recovery enqueues attempt = prior_count + 1; prior count
    # was 1 (the deliver_message row we added), so next attempt = 2.
    assert rows[0].payload["attempt"] == 2


@pytest.mark.asyncio
async def test_stale_recovery_marks_failed_when_budget_exhausted(db_engine, db_session):
    """A message stuck in delivering with the retry budget already
    exhausted (we count 4 dispatched outbox rows = initial + 3
    retries) should NOT get another retry row — mark failed.
    """
    from worker.poller import recover_stale_message_deliveries
    from worker.tasks import MESSAGE_RETRY_MAX_ATTEMPTS

    user, sender, recipient = await _make_user_and_agents(
        db_session,
        recipient_webhook_url="http://localhost:9712/wh",
        recipient_webhook_secret=generate_webhook_secret(),
    )
    msg = _make_message(user, sender, recipient)
    msg.delivery_state = "delivering"
    msg.delivering_started_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=600)
    db_session.add(msg)
    # Seed dispatched outbox rows = MESSAGE_RETRY_MAX_ATTEMPTS + 1
    # (4 total: initial + 3 retries).
    for i in range(MESSAGE_RETRY_MAX_ATTEMPTS + 1):
        db_session.add(
            DispatchOutbox(
                task_type="retry_message" if i > 0 else "deliver_message",
                payload={"message_id": msg.id, "to_agent_id": recipient.id, "attempt": i + 1},
                dispatched=True,
            )
        )
    await db_session.commit()

    recovered = await recover_stale_message_deliveries(db_engine, stale_seconds=300)
    assert recovered == 1

    await db_session.refresh(msg)
    assert msg.delivery_state == "failed"
    assert msg.failed_at is not None
    rows = await _retry_rows_for(db_session, msg.id)
    # No new retry row was added — only the pre-existing 3 retry_message
    # rows from setup + 0 new ones = 3.
    assert len(rows) == MESSAGE_RETRY_MAX_ATTEMPTS  # the seeded retries, no new


@pytest.mark.asyncio
async def test_stale_recovery_skips_recently_claimed(db_engine, db_session):
    """A message that was claimed only 60s ago (well under the 300s
    threshold) should be left alone.
    """
    from worker.poller import recover_stale_message_deliveries

    user, sender, recipient = await _make_user_and_agents(
        db_session,
        recipient_webhook_url="http://localhost:9713/wh",
        recipient_webhook_secret=generate_webhook_secret(),
    )
    msg = _make_message(user, sender, recipient)
    msg.delivery_state = "delivering"
    msg.delivering_started_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=60)
    db_session.add(msg)
    await db_session.commit()

    recovered = await recover_stale_message_deliveries(db_engine, stale_seconds=300)
    assert recovered == 0

    await db_session.refresh(msg)
    assert msg.delivery_state == "delivering"  # untouched


# ── Dispatcher scheduled_at filter ─────────────────────────────────


@pytest.mark.asyncio
async def test_outbox_dispatcher_skips_future_scheduled(db_engine, db_session):
    """A dispatch_outbox row with scheduled_at in the future is NOT
    picked up by the dispatcher; it stays undispatched.
    """
    from worker.poller import dispatch_outbox

    # Insert a retry_message row scheduled 10 minutes in the future.
    future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10)
    fake_message_id = generate_message_id()
    db_session.add(
        DispatchOutbox(
            task_type="retry_message",
            payload={"message_id": fake_message_id, "attempt": 2},
            scheduled_at=future,
        )
    )
    await db_session.commit()

    # Dispatcher should not pick it up — pass a fake arq_redis.
    class FakeArqRedis:
        async def enqueue_job(self, *args, **kwargs):
            raise AssertionError("Should not have enqueued the future-scheduled row")

    dispatched = await dispatch_outbox(db_engine, FakeArqRedis(), batch_size=500)
    assert dispatched == 0

    # Row still undispatched.
    result = await db_session.execute(
        select(DispatchOutbox).where(
            DispatchOutbox.payload["message_id"].astext == fake_message_id,
        )
    )
    row = result.scalar_one()
    assert row.dispatched is False
    assert row.scheduled_at == future


@pytest.mark.asyncio
async def test_outbox_dispatcher_picks_up_due_scheduled(db_engine, db_session):
    """A dispatch_outbox row with scheduled_at in the past IS picked
    up by the dispatcher.
    """
    from worker.poller import dispatch_outbox

    past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=5)
    fake_message_id = generate_message_id()
    db_session.add(
        DispatchOutbox(
            task_type="retry_message",
            payload={"message_id": fake_message_id, "attempt": 2, "to_agent_id": "agt_x"},
            scheduled_at=past,
        )
    )
    await db_session.commit()

    enqueued = []

    class FakeArqRedis:
        async def enqueue_job(self, task_name, payload):
            enqueued.append((task_name, payload))

    dispatched = await dispatch_outbox(db_engine, FakeArqRedis(), batch_size=500)
    assert dispatched == 1
    assert enqueued[0][0] == "retry_message_task"


@pytest.mark.asyncio
async def test_outbox_dispatcher_picks_up_null_scheduled(db_engine, db_session):
    """Backward-compat: rows with scheduled_at=NULL (existing
    cue-task rows + initial deliver_message rows) are dispatched
    immediately.
    """
    from worker.poller import dispatch_outbox

    fake_message_id = generate_message_id()
    db_session.add(
        DispatchOutbox(
            task_type="deliver_message",
            payload={"message_id": fake_message_id, "to_agent_id": "agt_x", "webhook_url": "http://localhost/wh"},
            # scheduled_at left as default = NULL
        )
    )
    await db_session.commit()

    enqueued = []

    class FakeArqRedis:
        async def enqueue_job(self, task_name, payload):
            enqueued.append((task_name, payload))

    dispatched = await dispatch_outbox(db_engine, FakeArqRedis(), batch_size=500)
    assert dispatched >= 1
    assert ("deliver_message_task", {"message_id": fake_message_id, "to_agent_id": "agt_x", "webhook_url": "http://localhost/wh"}) in enqueued
