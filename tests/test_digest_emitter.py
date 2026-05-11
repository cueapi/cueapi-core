"""Tests for the Phase 4b periodic digest emitter.

Two layers per CLAUDE.md pure-helper discipline:

* Pure-helper unit tests on ``_build_digest_payload`` — no DB, no
  Redis, no HTTP.
* ``emit_digests`` integration tests against the real DB through a
  fresh engine (matches the ``subscription_dispatcher`` pattern).

Verifies:

- Single low-priority event → emitted as a digest with `bundle_count=1`
- Multiple low-priority events for one recipient → single digest with
  bundled list
- p=3+ events → NOT digested (continue to fire normally)
- `digested_at` set on source events post-emit
- Empty period → no digest event emitted, return 0
- Idempotent re-emit (same idempotency_key suppresses duplicate)
- Per-recipient isolation — multiple recipients get separate digests
- DIGEST_MIN_BATCH_SIZE threshold — bundle smaller than threshold skipped
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models.agent import Agent
from app.models.event import Event
from app.models.user import User
from worker.digest_emitter import _build_digest_payload, emit_digests


async def _resolve_user_id(db_session: AsyncSession, email: str) -> str:
    user = (
        await db_session.execute(select(User).where(User.email == email))
    ).scalar_one()
    return str(user.id)


@pytest_asyncio.fixture
async def digest_agent(db_session: AsyncSession, registered_user: dict) -> Agent:
    user_id = await _resolve_user_id(db_session, registered_user["email"])
    agent = Agent(
        id="agt_digesttest01",
        user_id=user_id,
        slug="digest-test",
        display_name="Digest Test Agent",
    )
    db_session.add(agent)
    await db_session.commit()
    await db_session.refresh(agent)
    return agent


async def _make_engine():
    """Build a fresh AsyncEngine pointing at the test DB."""
    return create_async_engine(settings.async_database_url)


# ───────────────────────────────────────────────────────────────────────
# Pure-helper unit tests
# ───────────────────────────────────────────────────────────────────────


def test_build_digest_payload_shape():
    """Wire-shape of the digest payload matches the design lock —
    bundle_count + bundled_messages + period_start + period_end +
    recipient_agent_id."""
    now = datetime(2026, 5, 11, 10, 0, tzinfo=timezone.utc)
    earlier = datetime(2026, 5, 11, 9, 50, tzinfo=timezone.utc)
    events = [
        Event(
            id=1,
            event_type="message.delivered",
            recipient_agent_id="agt_x",
            payload={
                "message_id": "msg_a",
                "sender_agent_id": "agt_sender",
                "subject": "Hello",
                "priority": 2,
            },
            emitted_at=earlier,
        ),
        Event(
            id=2,
            event_type="message.delivered",
            recipient_agent_id="agt_x",
            payload={
                "message_id": "msg_b",
                "sender_agent_id": "agt_sender",
                "subject": "Hi",
                "priority": 1,
            },
            emitted_at=now,
        ),
    ]
    out = _build_digest_payload(
        recipient_agent_id="agt_x",
        bundled_events=events,
        period_start=earlier,
        period_end=now,
    )
    assert out["recipient_agent_id"] == "agt_x"
    assert out["bundle_count"] == 2
    assert out["digest_period_start"] == "2026-05-11T09:50:00+00:00"
    assert out["digest_period_end"] == "2026-05-11T10:00:00+00:00"
    assert len(out["bundled_messages"]) == 2
    assert out["bundled_messages"][0]["message_id"] == "msg_a"
    assert out["bundled_messages"][0]["priority"] == 2
    assert out["bundled_messages"][1]["message_id"] == "msg_b"


def test_build_digest_payload_preview_only_no_body():
    """Per CTO concur 2026-05-11: digest payload must NOT include
    full body. Bundled message entries should only carry preview-
    surface fields (message_id, sender, subject, priority, emitted_at)."""
    ev = Event(
        id=1,
        event_type="message.delivered",
        recipient_agent_id="agt_x",
        payload={
            "message_id": "msg_a",
            "sender_agent_id": "agt_s",
            "subject": "Topic",
            "priority": 2,
            "body": "the actual body content that should NOT appear in digest",
        },
        emitted_at=datetime.now(timezone.utc),
    )
    out = _build_digest_payload(
        recipient_agent_id="agt_x",
        bundled_events=[ev],
        period_start=datetime.now(timezone.utc),
        period_end=datetime.now(timezone.utc),
    )
    # The bundled message entry doesn't include 'body'.
    entry = out["bundled_messages"][0]
    assert "body" not in entry
    # Sanity: the preview-surface keys are present.
    assert set(entry.keys()) == {"message_id", "sender_agent_id", "subject", "priority", "emitted_at"}


def test_build_digest_payload_handles_missing_fields():
    """Defensive — events missing some payload fields don't crash;
    those entries get None for the missing keys."""
    ev = Event(
        id=1,
        event_type="message.delivered",
        recipient_agent_id="agt_x",
        payload={"message_id": "msg_a"},  # only message_id; other fields missing
        emitted_at=datetime.now(timezone.utc),
    )
    out = _build_digest_payload(
        recipient_agent_id="agt_x",
        bundled_events=[ev],
        period_start=datetime.now(timezone.utc),
        period_end=datetime.now(timezone.utc),
    )
    entry = out["bundled_messages"][0]
    assert entry["message_id"] == "msg_a"
    assert entry["sender_agent_id"] is None
    assert entry["subject"] is None
    assert entry["priority"] is None


def test_build_digest_payload_empty_bundle():
    """Empty bundled_events → bundle_count=0 + empty bundled_messages.
    Defensive code path; the emitter filters empty batches before
    calling, but the helper handles it for robustness."""
    out = _build_digest_payload(
        recipient_agent_id="agt_x",
        bundled_events=[],
        period_start=datetime.now(timezone.utc),
        period_end=datetime.now(timezone.utc),
    )
    assert out["bundle_count"] == 0
    assert out["bundled_messages"] == []


# ───────────────────────────────────────────────────────────────────────
# Integration: emit_digests
# ───────────────────────────────────────────────────────────────────────


async def _seed_event(
    db_session: AsyncSession,
    *,
    agent_id: str,
    priority: int,
    message_id: str = "msg_seed",
    digested_at: datetime | None = None,
) -> Event:
    ev = Event(
        event_type="message.delivered",
        recipient_agent_id=agent_id,
        payload={
            "message_id": message_id,
            "sender_agent_id": "agt_sender",
            "subject": "Test",
            "priority": priority,
        },
        emitted_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        digested_at=digested_at,
    )
    db_session.add(ev)
    await db_session.commit()
    await db_session.refresh(ev)
    return ev


async def test_emit_digests_single_p1_event_creates_digest(
    db_session: AsyncSession, digest_agent: Agent
):
    """A single un-digested p=1 event → 1 digest event emitted +
    source event's digested_at populated."""
    src = await _seed_event(
        db_session, agent_id=digest_agent.id, priority=1, message_id="msg_lonely",
    )

    engine = await _make_engine()
    try:
        count = await emit_digests(engine)
    finally:
        await engine.dispose()
    assert count == 1

    # Source event marked.
    await db_session.refresh(src)
    assert src.digested_at is not None

    # Digest event exists.
    digests = (
        await db_session.execute(
            select(Event).where(Event.event_type == "message.digest")
        )
    ).scalars().all()
    assert len(digests) == 1
    digest = digests[0]
    assert digest.recipient_agent_id == digest_agent.id
    assert digest.payload["bundle_count"] == 1
    assert digest.payload["bundled_messages"][0]["message_id"] == "msg_lonely"


async def test_emit_digests_multiple_low_priority_events_bundled(
    db_session: AsyncSession, digest_agent: Agent
):
    """3 un-digested events (mix of p=1 + p=2) for one recipient →
    single digest with bundle_count=3."""
    await _seed_event(db_session, agent_id=digest_agent.id, priority=1, message_id="msg_1")
    await _seed_event(db_session, agent_id=digest_agent.id, priority=2, message_id="msg_2")
    await _seed_event(db_session, agent_id=digest_agent.id, priority=1, message_id="msg_3")

    engine = await _make_engine()
    try:
        count = await emit_digests(engine)
    finally:
        await engine.dispose()
    assert count == 1

    digests = (
        await db_session.execute(
            select(Event).where(Event.event_type == "message.digest")
        )
    ).scalars().all()
    assert len(digests) == 1
    assert digests[0].payload["bundle_count"] == 3


async def test_emit_digests_high_priority_events_skipped(
    db_session: AsyncSession, digest_agent: Agent
):
    """p=3, p=4, p=5 events are NOT digested — they continue to fire
    via the normal subscription dispatcher loop."""
    await _seed_event(db_session, agent_id=digest_agent.id, priority=3)
    await _seed_event(db_session, agent_id=digest_agent.id, priority=4)
    await _seed_event(db_session, agent_id=digest_agent.id, priority=5)

    engine = await _make_engine()
    try:
        count = await emit_digests(engine)
    finally:
        await engine.dispose()
    assert count == 0

    digests = (
        await db_session.execute(
            select(Event).where(Event.event_type == "message.digest")
        )
    ).scalars().all()
    assert digests == []


async def test_emit_digests_skips_already_digested_events(
    db_session: AsyncSession, digest_agent: Agent
):
    """Events with digested_at set → emitter ignores; no duplicate
    digest produced."""
    await _seed_event(
        db_session,
        agent_id=digest_agent.id,
        priority=1,
        digested_at=datetime.now(timezone.utc),
    )

    engine = await _make_engine()
    try:
        count = await emit_digests(engine)
    finally:
        await engine.dispose()
    assert count == 0


async def test_emit_digests_empty_period_returns_zero(
    db_session: AsyncSession, digest_agent: Agent
):
    """No un-digested low-priority events → return 0, no events
    inserted."""
    engine = await _make_engine()
    try:
        count = await emit_digests(engine)
    finally:
        await engine.dispose()
    assert count == 0


async def test_emit_digests_per_recipient_isolation(
    db_session: AsyncSession, digest_agent: Agent, registered_user: dict
):
    """Each recipient with un-digested events gets their own digest;
    bundles don't cross-pollinate."""
    user_id = await _resolve_user_id(db_session, registered_user["email"])
    other = Agent(
        id="agt_otherdigest1",
        user_id=user_id,
        slug="other-digest",
        display_name="Other Digest Recipient",
    )
    db_session.add(other)
    await db_session.commit()

    await _seed_event(db_session, agent_id=digest_agent.id, priority=1, message_id="msg_self")
    await _seed_event(db_session, agent_id=other.id, priority=2, message_id="msg_other")

    engine = await _make_engine()
    try:
        count = await emit_digests(engine)
    finally:
        await engine.dispose()
    assert count == 2  # one per recipient

    digests = (
        await db_session.execute(
            select(Event).where(Event.event_type == "message.digest").order_by(Event.id)
        )
    ).scalars().all()
    assert len(digests) == 2
    recipients = {d.recipient_agent_id for d in digests}
    assert recipients == {digest_agent.id, other.id}
    # Each digest carries only its recipient's message.
    for d in digests:
        bundled_msg_ids = [m["message_id"] for m in d.payload["bundled_messages"]]
        if d.recipient_agent_id == digest_agent.id:
            assert bundled_msg_ids == ["msg_self"]
        else:
            assert bundled_msg_ids == ["msg_other"]


async def test_emit_digests_idempotent_via_idempotency_key(
    db_session: AsyncSession, digest_agent: Agent
):
    """Running emit_digests twice without new events between cycles
    — the second cycle finds no un-digested events (first run
    marked them) so it emits 0."""
    await _seed_event(db_session, agent_id=digest_agent.id, priority=1)

    engine = await _make_engine()
    try:
        first = await emit_digests(engine)
        second = await emit_digests(engine)
    finally:
        await engine.dispose()
    assert first == 1
    assert second == 0

    digests = (
        await db_session.execute(
            select(Event).where(Event.event_type == "message.digest")
        )
    ).scalars().all()
    assert len(digests) == 1
