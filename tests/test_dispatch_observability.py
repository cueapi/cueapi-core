"""Tests for Phase 4c — dispatch cycle observability.

Verifies the structured log emission + per-tier counters in
``dispatch_subscription_events``:

- Tier-fired counts increment for successful fires
- Tier-deferred counts increment for p=4 events caught by debounce
- ``subscription_dispatch_cycle`` log event_type emitted with both
  dicts when activity occurs
- No log emission on empty cycles (signal-to-noise discipline)

Uses the same engine + db_session pattern as
test_subscription_dispatcher.py to avoid the standalone DB-state quirk.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import patch

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.models.agent import Agent
from app.models.event import Event
from app.models.subscription import Subscription
from app.models.user import User
from worker.subscription_dispatcher import dispatch_subscription_events


async def _resolve_user_id(db_session: AsyncSession, email: str) -> str:
    user = (
        await db_session.execute(select(User).where(User.email == email))
    ).scalar_one()
    return str(user.id)


@pytest_asyncio.fixture
async def obs_agent(db_session: AsyncSession, registered_user: dict) -> Agent:
    user_id = await _resolve_user_id(db_session, registered_user["email"])
    agent = Agent(
        id="agt_obstest00001",
        user_id=user_id,
        slug="obs-test",
        display_name="Observability Test",
    )
    db_session.add(agent)
    await db_session.commit()
    await db_session.refresh(agent)
    return agent


class FakeRedis:
    def __init__(self):
        self.store = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value


async def _make_engine():
    return create_async_engine(settings.async_database_url)


async def test_dispatch_cycle_logs_tier_breakdown_on_activity(
    db_session: AsyncSession, obs_agent: Agent, caplog
):
    """When the dispatch loop fires events, it emits one structured
    log line with `event_type=subscription_dispatch_cycle` carrying
    the per-tier counter dicts."""
    sub = Subscription(
        subscriber_agent_id=obs_agent.id,
        event_type="message.delivered",
        delivery_target="webhook",
        webhook_url="https://example.com/hook",
        webhook_secret="whsec_test",
    )
    db_session.add(sub)
    db_session.add(
        Event(
            event_type="message.delivered",
            recipient_agent_id=obs_agent.id,
            payload={"priority": 5, "subject": "urgent"},
            emitted_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    async def fake_deliver(*, url, secret, body):
        return (True, 200)

    engine = await _make_engine()
    try:
        with caplog.at_level(logging.INFO, logger="worker.subscription_dispatcher"):
            with patch(
                "worker.subscription_dispatcher._deliver_webhook",
                side_effect=fake_deliver,
            ):
                await dispatch_subscription_events(engine, redis=FakeRedis())
    finally:
        await engine.dispose()

    # Find the cycle-summary log record.
    cycle_records = [
        r for r in caplog.records
        if getattr(r, "event_type", None) == "subscription_dispatch_cycle"
    ]
    assert len(cycle_records) == 1
    rec = cycle_records[0]
    assert rec.tier_fired == {"1": 0, "2": 0, "3": 0, "4": 0, "5": 1}
    assert rec.tier_deferred == {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0}
    assert rec.total_fired == 1
    assert rec.total_deferred == 0
    assert rec.attempts == 1


async def test_dispatch_cycle_no_log_on_empty_activity(
    db_session: AsyncSession, obs_agent: Agent, caplog
):
    """Empty cycles (no subs / no events) emit NO log line.
    Signal-to-noise discipline — log only when there's something to
    report."""
    # No subscriptions seeded. Dispatcher finds nothing.
    engine = await _make_engine()
    try:
        with caplog.at_level(logging.INFO, logger="worker.subscription_dispatcher"):
            await dispatch_subscription_events(engine, redis=FakeRedis())
    finally:
        await engine.dispose()

    cycle_records = [
        r for r in caplog.records
        if getattr(r, "event_type", None) == "subscription_dispatch_cycle"
    ]
    assert cycle_records == []


async def test_dispatch_cycle_logs_deferred_when_debounced(
    db_session: AsyncSession, obs_agent: Agent, caplog
):
    """p=4 event deferred by debounce → tier_deferred counter
    increments + reported in the cycle log."""
    sub = Subscription(
        subscriber_agent_id=obs_agent.id,
        event_type="message.delivered",
        delivery_target="webhook",
        webhook_url="https://example.com/hook",
        webhook_secret="whsec_test",
    )
    db_session.add(sub)

    # Seed events: one p=3 (fires) + one p=4 (deferred since redis
    # marker indicates recent fire).
    db_session.add(
        Event(
            event_type="message.delivered",
            recipient_agent_id=obs_agent.id,
            payload={"priority": 3},
            emitted_at=datetime.now(timezone.utc),
        )
    )
    db_session.add(
        Event(
            event_type="message.delivered",
            recipient_agent_id=obs_agent.id,
            payload={"priority": 4},
            emitted_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    redis = FakeRedis()
    # Pre-stamp the p=4 debounce marker so the p=4 event gets deferred.
    import time
    redis.store[f"priority_4_debounce:{obs_agent.id}"] = str(time.time())

    async def fake_deliver(*, url, secret, body):
        return (True, 200)

    engine = await _make_engine()
    try:
        with caplog.at_level(logging.INFO, logger="worker.subscription_dispatcher"):
            with patch(
                "worker.subscription_dispatcher._deliver_webhook",
                side_effect=fake_deliver,
            ):
                await dispatch_subscription_events(engine, redis=redis)
    finally:
        await engine.dispose()

    cycle_records = [
        r for r in caplog.records
        if getattr(r, "event_type", None) == "subscription_dispatch_cycle"
    ]
    assert len(cycle_records) == 1
    rec = cycle_records[0]
    # p=3 fired; p=4 deferred.
    assert rec.tier_fired["3"] == 1
    assert rec.tier_deferred["4"] == 1
    assert rec.total_fired == 1
    assert rec.total_deferred == 1


async def test_dispatch_cycle_no_log_when_fire_fails(
    db_session: AsyncSession, obs_agent: Agent, caplog
):
    """When webhook delivery FAILS (5xx), counters stay 0 (we only
    increment tier_fired on success). The cycle log still fires
    because attempts > 0 ... wait, actually attempts increments for
    failed deliveries too. So the log emits with tier_fired all
    zeros + total_fired=0 but total_deferred=0 too. Verify the
    behavior: log emits ONLY when fired OR deferred > 0."""
    sub = Subscription(
        subscriber_agent_id=obs_agent.id,
        event_type="message.delivered",
        delivery_target="webhook",
        webhook_url="https://example.com/hook",
        webhook_secret="whsec_test",
    )
    db_session.add(sub)
    db_session.add(
        Event(
            event_type="message.delivered",
            recipient_agent_id=obs_agent.id,
            payload={"priority": 3},
            emitted_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    async def fake_deliver(*, url, secret, body):
        return (False, 500)  # 5xx failure

    engine = await _make_engine()
    try:
        with caplog.at_level(logging.INFO, logger="worker.subscription_dispatcher"):
            with patch(
                "worker.subscription_dispatcher._deliver_webhook",
                side_effect=fake_deliver,
            ):
                await dispatch_subscription_events(engine, redis=FakeRedis())
    finally:
        await engine.dispose()

    cycle_records = [
        r for r in caplog.records
        if getattr(r, "event_type", None) == "subscription_dispatch_cycle"
    ]
    # Failed-only cycle → all counters stay zero → no log emission
    # per the gate `total_fired > 0 or total_deferred > 0`.
    assert cycle_records == []
