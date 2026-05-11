"""Tests for the per-tier subscription dispatcher policy (Phase 4a).

Two layers:

* Pure-helper unit tests on ``_event_priority`` + ``_debounce_key``
  — no DB, no Redis, no HTTP.
* ``apply_tier_policy`` + ``stamp_dispatch_markers`` integration with
  a fake-redis stand-in — exercises every branch (p=3/5 pass-through,
  p=4 debounce hit, p=4 debounce miss, Redis-down fallback, malformed
  marker fallback, mixed-tier batch).

Per CLAUDE.md pure-helper extraction discipline: every branch in the
policy module gets a dedicated direct-call test so pytest-cov sees
it without going through the ASGI dispatch layer.
"""
from __future__ import annotations

from typing import Any, Dict, Optional
from unittest.mock import patch

import pytest

from worker.subscription_dispatcher_policy import (
    DEBOUNCE_KEY_PREFIX,
    PRIORITY_ARCHIVE,
    PRIORITY_DEFAULT,
    PRIORITY_HIGH,
    PRIORITY_LOW,
    PRIORITY_URGENT,
    _debounce_key,
    _event_priority,
    _is_p4_debounced,
    _stamp_p4_fire,
    apply_tier_policy,
    stamp_dispatch_markers,
)


# ───────────────────────────────────────────────────────────────────────
# FakeRedis stub — minimal in-memory + async get/set with optional
# side-effects for Redis-down simulation.
# ───────────────────────────────────────────────────────────────────────


class FakeRedis:
    """Minimal stand-in for the Redis async client.

    Supports: ``get(key)``, ``set(key, value, ex=ttl)``. Real Redis
    behavior on TTL is not simulated (TTL is recorded but doesn't
    auto-expire in the test); tests that need expiry use the
    monkeypatched ``_now_seconds`` clock.
    """

    def __init__(self):
        self.store: Dict[str, str] = {}
        self.ttls: Dict[str, int] = {}
        self.fail_on_get = False
        self.fail_on_set = False

    async def get(self, key: str) -> Optional[str]:
        if self.fail_on_get:
            raise RuntimeError("simulated Redis down on get")
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: Optional[int] = None) -> None:
        if self.fail_on_set:
            raise RuntimeError("simulated Redis down on set")
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex


def _evt(*, priority: int = 3, eid: int = 1, payload_extra: Optional[Dict] = None) -> Any:
    """Construct a dict-shaped event suitable for the helper.

    The helper accepts either ORM Event objects or plain dicts (per
    its docstring) — using dicts keeps the test free of DB setup.
    """
    payload = {"priority": priority}
    if payload_extra:
        payload.update(payload_extra)
    return {"id": eid, "payload": payload, "event_type": "message.delivered"}


# ───────────────────────────────────────────────────────────────────────
# _event_priority — branches
# ───────────────────────────────────────────────────────────────────────


def test_event_priority_extracts_from_dict():
    assert _event_priority(_evt(priority=5)) == 5
    assert _event_priority(_evt(priority=1)) == 1


def test_event_priority_defaults_when_missing():
    """No `priority` field in payload → default tier."""
    assert _event_priority({"id": 1, "payload": {}}) == PRIORITY_DEFAULT


def test_event_priority_defaults_on_non_int():
    """priority="high" (string) → default tier (defensive)."""
    assert _event_priority({"payload": {"priority": "high"}}) == PRIORITY_DEFAULT


def test_event_priority_defaults_on_out_of_range():
    """priority=99 → default tier; priority=0 → default tier."""
    assert _event_priority({"payload": {"priority": 99}}) == PRIORITY_DEFAULT
    assert _event_priority({"payload": {"priority": 0}}) == PRIORITY_DEFAULT


def test_event_priority_handles_missing_payload():
    """No payload attr at all → default tier."""
    assert _event_priority({}) == PRIORITY_DEFAULT


def test_debounce_key_format():
    assert _debounce_key("agt_abc") == f"{DEBOUNCE_KEY_PREFIX}:agt_abc"


# ───────────────────────────────────────────────────────────────────────
# _is_p4_debounced — branches
# ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_p4_debounced_false_when_no_marker():
    """No prior fire recorded → not debounced."""
    redis = FakeRedis()
    assert await _is_p4_debounced(redis, "agt_x") is False


@pytest.mark.asyncio
async def test_p4_debounced_true_within_window():
    """Fire within the window → debounced."""
    redis = FakeRedis()
    # Pretend a fire just happened.
    with patch(
        "worker.subscription_dispatcher_policy._now_seconds",
        return_value=1000.0,
    ):
        await _stamp_p4_fire(redis, "agt_x")
    # Now check — clock advanced 1s (well within 2s default window).
    with patch(
        "worker.subscription_dispatcher_policy._now_seconds",
        return_value=1001.0,
    ):
        assert await _is_p4_debounced(redis, "agt_x") is True


@pytest.mark.asyncio
async def test_p4_debounced_false_outside_window():
    """Fire 3s ago → window elapsed → not debounced."""
    redis = FakeRedis()
    with patch(
        "worker.subscription_dispatcher_policy._now_seconds",
        return_value=1000.0,
    ):
        await _stamp_p4_fire(redis, "agt_x")
    with patch(
        "worker.subscription_dispatcher_policy._now_seconds",
        return_value=1003.5,
    ):
        assert await _is_p4_debounced(redis, "agt_x") is False


@pytest.mark.asyncio
async def test_p4_debounced_false_on_redis_down():
    """Redis errors out → not debounced (fail-open allows fire)."""
    redis = FakeRedis()
    redis.fail_on_get = True
    assert await _is_p4_debounced(redis, "agt_x") is False


@pytest.mark.asyncio
async def test_p4_debounced_false_on_malformed_marker():
    """Marker isn't a valid float → not debounced."""
    redis = FakeRedis()
    redis.store[_debounce_key("agt_x")] = "not-a-number"
    assert await _is_p4_debounced(redis, "agt_x") is False


# ───────────────────────────────────────────────────────────────────────
# _stamp_p4_fire — Redis-down doesn't raise
# ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stamp_p4_fire_handles_redis_down():
    """Redis errors on SET → logged but doesn't raise."""
    redis = FakeRedis()
    redis.fail_on_set = True
    # Should not raise.
    await _stamp_p4_fire(redis, "agt_x")


@pytest.mark.asyncio
async def test_stamp_p4_fire_sets_ttl():
    """The marker key is set with a TTL > debounce window."""
    redis = FakeRedis()
    await _stamp_p4_fire(redis, "agt_x")
    key = _debounce_key("agt_x")
    assert key in redis.store
    # TTL is debounce_seconds + 1, so >= 3 for the default 2s window.
    assert redis.ttls[key] >= 3


# ───────────────────────────────────────────────────────────────────────
# apply_tier_policy — branches
# ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_tier_policy_empty_batch():
    """Empty events list → empty results, no Redis call needed."""
    redis = FakeRedis()
    to_fire, deferred = await apply_tier_policy(
        redis, subscriber_agent_id="agt_x", events=[]
    )
    assert to_fire == []
    assert deferred == []


@pytest.mark.asyncio
async def test_apply_tier_policy_no_p4_events_skips_redis():
    """No p=4 events in batch → no Redis check, all pass through."""
    redis = FakeRedis()
    events = [_evt(priority=5, eid=1), _evt(priority=3, eid=2)]
    to_fire, deferred = await apply_tier_policy(
        redis, subscriber_agent_id="agt_x", events=events
    )
    assert to_fire == events
    assert deferred == []
    # Redis store still empty — no `get` call should have been needed.
    assert _debounce_key("agt_x") not in redis.store


@pytest.mark.asyncio
async def test_apply_tier_policy_p4_passes_when_not_debounced():
    """p=4 event + no prior fire → passes through."""
    redis = FakeRedis()
    events = [_evt(priority=PRIORITY_HIGH, eid=1)]
    to_fire, deferred = await apply_tier_policy(
        redis, subscriber_agent_id="agt_x", events=events
    )
    assert to_fire == events
    assert deferred == []


@pytest.mark.asyncio
async def test_apply_tier_policy_p4_defers_when_debounced():
    """p=4 event + prior fire within window → deferred."""
    redis = FakeRedis()
    # Pre-stamp a recent fire.
    with patch(
        "worker.subscription_dispatcher_policy._now_seconds",
        return_value=1000.0,
    ):
        await _stamp_p4_fire(redis, "agt_x")

    events = [_evt(priority=PRIORITY_HIGH, eid=1)]
    with patch(
        "worker.subscription_dispatcher_policy._now_seconds",
        return_value=1001.0,
    ):
        to_fire, deferred = await apply_tier_policy(
            redis, subscriber_agent_id="agt_x", events=events
        )
    assert to_fire == []
    assert deferred == events


@pytest.mark.asyncio
async def test_apply_tier_policy_mixed_batch_p5_passes_p4_defers():
    """Mixed-priority batch + recipient is debounced: p=5 + p=3 fire,
    only the p=4 gets deferred."""
    redis = FakeRedis()
    with patch(
        "worker.subscription_dispatcher_policy._now_seconds",
        return_value=1000.0,
    ):
        await _stamp_p4_fire(redis, "agt_x")

    events = [
        _evt(priority=PRIORITY_URGENT, eid=1),
        _evt(priority=PRIORITY_HIGH, eid=2),  # deferred
        _evt(priority=PRIORITY_DEFAULT, eid=3),
    ]
    with patch(
        "worker.subscription_dispatcher_policy._now_seconds",
        return_value=1001.0,
    ):
        to_fire, deferred = await apply_tier_policy(
            redis, subscriber_agent_id="agt_x", events=events
        )
    assert [e["id"] for e in to_fire] == [1, 3]
    assert [e["id"] for e in deferred] == [2]


@pytest.mark.asyncio
async def test_apply_tier_policy_p2_p1_pass_through_at_phase4a():
    """Phase 4a preserves v1 behavior for p=1/p=2 — they pass through.
    Phase 4b will swap them to digest emission."""
    redis = FakeRedis()
    events = [
        _evt(priority=PRIORITY_LOW, eid=1),
        _evt(priority=PRIORITY_ARCHIVE, eid=2),
    ]
    to_fire, deferred = await apply_tier_policy(
        redis, subscriber_agent_id="agt_x", events=events
    )
    assert to_fire == events
    assert deferred == []


@pytest.mark.asyncio
async def test_apply_tier_policy_redis_down_fires_everything():
    """Redis errors on GET → all events fire (no silent suppression)."""
    redis = FakeRedis()
    redis.fail_on_get = True
    events = [_evt(priority=PRIORITY_HIGH, eid=1)]
    to_fire, deferred = await apply_tier_policy(
        redis, subscriber_agent_id="agt_x", events=events
    )
    assert to_fire == events
    assert deferred == []


# ───────────────────────────────────────────────────────────────────────
# stamp_dispatch_markers — branches
# ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stamp_markers_p4_in_batch_stamps():
    redis = FakeRedis()
    events = [_evt(priority=PRIORITY_HIGH, eid=1)]
    await stamp_dispatch_markers(
        redis, subscriber_agent_id="agt_x", events=events
    )
    assert _debounce_key("agt_x") in redis.store


@pytest.mark.asyncio
async def test_stamp_markers_no_p4_skips_redis():
    """If no p=4 events fired, no need to stamp."""
    redis = FakeRedis()
    events = [_evt(priority=PRIORITY_DEFAULT, eid=1)]
    await stamp_dispatch_markers(
        redis, subscriber_agent_id="agt_x", events=events
    )
    assert _debounce_key("agt_x") not in redis.store
