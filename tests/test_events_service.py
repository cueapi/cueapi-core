"""Unit + integration tests for ``app/services/events_service.py``.

Pure-helper extraction pattern (per CLAUDE.md patch-coverage mandate):
the validation branches (``_validate_event_type``,
``_validate_delivery_target_combo``, SSRF check inside
``create_subscription``) get their own dedicated tests so every
error-return path is covered, not just the happy path.

Tests use the existing async session fixture from conftest.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.event import Event
from app.models.subscription import Subscription
from app.models.user import User
from app.services import events_service
from app.services.events_service import (
    InvalidDeliveryTargetError,
    InvalidWebhookUrlError,
    KNOWN_EVENT_TYPES,
    UnknownEventTypeError,
    _validate_delivery_target_combo,
    _validate_event_type,
    create_subscription,
    detach_subscription,
    emit_event,
    list_subscriptions,
    pull_events,
)


# asyncio_mode = auto in pytest.ini — async tests run automatically.


# ───────────────────────────────────────────────────────────────────────
# Helper fixtures
# ───────────────────────────────────────────────────────────────────────

async def _resolve_user_id(db_session: AsyncSession, email: str) -> str:
    """Register-response doesn't include user_id; look it up by email."""
    user = (
        await db_session.execute(select(User).where(User.email == email))
    ).scalar_one()
    return str(user.id)


@pytest_asyncio.fixture
async def stub_agent(db_session: AsyncSession, registered_user: dict) -> Agent:
    """An agent owned by the registered_user fixture, persisted."""
    user_id = await _resolve_user_id(db_session, registered_user["email"])
    agent = Agent(
        id="agt_evttest0001",
        user_id=user_id,
        slug="event-test",
        display_name="Event Test Agent",
    )
    db_session.add(agent)
    await db_session.commit()
    await db_session.refresh(agent)
    return agent


# ───────────────────────────────────────────────────────────────────────
# Pure-helper validation branches (no DB)
# ───────────────────────────────────────────────────────────────────────

def test_validate_event_type_accepts_known():
    """Known event types pass validation silently."""
    for et in KNOWN_EVENT_TYPES:
        _validate_event_type(et)  # no exception


def test_validate_event_type_rejects_unknown():
    """Unknown event types raise UnknownEventTypeError."""
    with pytest.raises(UnknownEventTypeError) as exc_info:
        _validate_event_type("totally.made.up.event")
    assert "totally.made.up.event" in str(exc_info.value)
    assert exc_info.value.code == "unknown_event_type"
    assert exc_info.value.status == 400


def test_validate_delivery_target_pull_accepts_no_url():
    _validate_delivery_target_combo("pull", None)  # no exception


def test_validate_delivery_target_pull_rejects_url():
    with pytest.raises(InvalidDeliveryTargetError) as exc_info:
        _validate_delivery_target_combo("pull", "https://example.com/hook")
    assert "must not include webhook_url" in str(exc_info.value)


def test_validate_delivery_target_webhook_accepts_url():
    _validate_delivery_target_combo("webhook", "https://example.com/hook")


def test_validate_delivery_target_webhook_rejects_no_url():
    with pytest.raises(InvalidDeliveryTargetError) as exc_info:
        _validate_delivery_target_combo("webhook", None)
    assert "requires webhook_url" in str(exc_info.value)


def test_validate_delivery_target_webhook_rejects_empty_url():
    """Empty-string webhook_url is rejected — same shape as None."""
    with pytest.raises(InvalidDeliveryTargetError):
        _validate_delivery_target_combo("webhook", "")


def test_validate_delivery_target_rejects_unknown_target():
    with pytest.raises(InvalidDeliveryTargetError) as exc_info:
        _validate_delivery_target_combo("inbox", None)
    assert "must be 'pull' or 'webhook'" in str(exc_info.value)


def test_known_event_types_contains_message_delivered():
    """v0.1 registry must include message.delivered (PR-2a consumer)."""
    assert "message.delivered" in KNOWN_EVENT_TYPES


def test_known_event_types_contains_message_digest():
    """Phase 4b — message.digest must be in the registry so subscribers
    can subscribe to bundled summaries."""
    assert "message.digest" in KNOWN_EVENT_TYPES


def test_known_event_types_contains_turn_pass():
    """Item 2(a) (Backlog cmp1j1tt600040) — turn.pass must be in the
    registry for inbox-watcher recipes to subscribe + filter on it."""
    assert "turn.pass" in KNOWN_EVENT_TYPES


# ───────────────────────────────────────────────────────────────────────
# emit_event — happy + idempotency paths
# ───────────────────────────────────────────────────────────────────────

async def test_emit_event_creates_row(db_session: AsyncSession, stub_agent: Agent):
    event = await emit_event(
        db_session,
        event_type="message.delivered",
        recipient_agent_id=stub_agent.id,
        payload={"message_id": "msg_abc"},
    )
    await db_session.commit()

    assert event.id > 0
    assert event.event_type == "message.delivered"
    assert event.recipient_agent_id == stub_agent.id
    assert event.payload == {"message_id": "msg_abc"}
    assert event.idempotency_key is None


async def test_emit_event_idempotency_returns_existing(
    db_session: AsyncSession, stub_agent: Agent
):
    """Re-emit with same (event_type, idempotency_key) returns existing id."""
    first = await emit_event(
        db_session,
        event_type="message.delivered",
        recipient_agent_id=stub_agent.id,
        payload={"v": 1},
        idempotency_key="key-1",
    )
    await db_session.commit()

    second = await emit_event(
        db_session,
        event_type="message.delivered",
        recipient_agent_id=stub_agent.id,
        payload={"v": 2},  # different payload — should be ignored
        idempotency_key="key-1",
    )
    await db_session.commit()

    assert second.id == first.id
    # Existing row's payload is preserved (no overwrite on conflict).
    refreshed = (
        await db_session.execute(select(Event).where(Event.id == first.id))
    ).scalar_one()
    assert refreshed.payload == {"v": 1}


async def test_emit_event_distinct_keys_create_distinct_rows(
    db_session: AsyncSession, stub_agent: Agent
):
    first = await emit_event(
        db_session,
        event_type="message.delivered",
        recipient_agent_id=stub_agent.id,
        payload={},
        idempotency_key="key-a",
    )
    second = await emit_event(
        db_session,
        event_type="message.delivered",
        recipient_agent_id=stub_agent.id,
        payload={},
        idempotency_key="key-b",
    )
    await db_session.commit()
    assert first.id != second.id


# ───────────────────────────────────────────────────────────────────────
# pull_events — cursor pagination + filters
# ───────────────────────────────────────────────────────────────────────

async def test_pull_events_returns_empty_for_no_events(
    db_session: AsyncSession, stub_agent: Agent
):
    events, cursor, has_more = await pull_events(
        db_session, recipient_agent_id=stub_agent.id
    )
    assert events == []
    assert cursor is None
    assert has_more is False


async def test_pull_events_cursor_ordering(
    db_session: AsyncSession, stub_agent: Agent
):
    """Events come back ordered ASC by id; next_cursor advances."""
    for i in range(3):
        await emit_event(
            db_session,
            event_type="message.delivered",
            recipient_agent_id=stub_agent.id,
            payload={"i": i},
        )
    await db_session.commit()

    events, cursor, has_more = await pull_events(
        db_session, recipient_agent_id=stub_agent.id
    )
    assert len(events) == 3
    assert events[0].id < events[1].id < events[2].id
    assert cursor == events[2].id
    assert has_more is False


async def test_pull_events_resume_via_since(
    db_session: AsyncSession, stub_agent: Agent
):
    events_ids = []
    for _ in range(5):
        e = await emit_event(
            db_session,
            event_type="message.delivered",
            recipient_agent_id=stub_agent.id,
            payload={},
        )
        events_ids.append(e.id)
    await db_session.commit()

    second_page, _, _ = await pull_events(
        db_session, recipient_agent_id=stub_agent.id, since=events_ids[2]
    )
    # Only events with id > events_ids[2] = the last 2.
    assert [e.id for e in second_page] == events_ids[3:]


async def test_pull_events_has_more_when_limit_hit(
    db_session: AsyncSession, stub_agent: Agent
):
    for _ in range(3):
        await emit_event(
            db_session,
            event_type="message.delivered",
            recipient_agent_id=stub_agent.id,
            payload={},
        )
    await db_session.commit()

    events, cursor, has_more = await pull_events(
        db_session, recipient_agent_id=stub_agent.id, limit=2
    )
    assert len(events) == 2
    assert has_more is True
    assert cursor == events[1].id


async def test_pull_events_isolates_by_recipient(
    db_session: AsyncSession, stub_agent: Agent, registered_user: dict
):
    """Events for one agent are not visible to another."""
    user_id = await _resolve_user_id(db_session, registered_user["email"])
    other = Agent(
        id="agt_other00001",
        user_id=user_id,
        slug="other",
        display_name="Other Agent",
    )
    db_session.add(other)
    await db_session.commit()

    await emit_event(
        db_session,
        event_type="message.delivered",
        recipient_agent_id=stub_agent.id,
        payload={"for": "stub"},
    )
    await db_session.commit()

    events, _, _ = await pull_events(
        db_session, recipient_agent_id=other.id
    )
    assert events == []


async def test_pull_events_limit_clamped_at_max(
    db_session: AsyncSession, stub_agent: Agent
):
    """Server-side cap MAX_PULL_LIMIT applies even if caller asks higher."""
    # Only emit a few events; the limit clamp doesn't affect the
    # return count (no events to over-return). The test verifies the
    # function accepts a huge limit without erroring.
    await emit_event(
        db_session,
        event_type="message.delivered",
        recipient_agent_id=stub_agent.id,
        payload={},
    )
    await db_session.commit()
    events, _, _ = await pull_events(
        db_session,
        recipient_agent_id=stub_agent.id,
        limit=999_999,  # massively over MAX_PULL_LIMIT
    )
    assert len(events) == 1


async def test_pull_events_event_type_filter(
    db_session: AsyncSession, stub_agent: Agent
):
    """event_type filter restricts to matching rows. v0.1 only ships
    one type so the filter coverage is small but the branch must
    exist for v0.2+ event-type expansion."""
    await emit_event(
        db_session,
        event_type="message.delivered",
        recipient_agent_id=stub_agent.id,
        payload={},
    )
    await db_session.commit()

    # Matching filter returns the event.
    matching, _, _ = await pull_events(
        db_session,
        recipient_agent_id=stub_agent.id,
        event_type="message.delivered",
    )
    assert len(matching) == 1

    # Non-matching filter returns empty.
    other, _, _ = await pull_events(
        db_session,
        recipient_agent_id=stub_agent.id,
        event_type="some.other.type",  # not in registry but pull doesn't validate
    )
    assert other == []


# ───────────────────────────────────────────────────────────────────────
# create_subscription — happy + each error branch
# ───────────────────────────────────────────────────────────────────────

async def test_create_subscription_pull_happy_path(
    db_session: AsyncSession, stub_agent: Agent
):
    sub = await create_subscription(
        db_session,
        subscriber_agent_id=stub_agent.id,
        event_type="message.delivered",
        delivery_target="pull",
    )
    await db_session.commit()
    assert sub.id is not None
    assert sub.delivery_target == "pull"
    assert sub.webhook_url is None
    assert sub.webhook_secret is None
    assert sub.detached_at is None


async def test_create_subscription_webhook_happy_path(
    db_session: AsyncSession, stub_agent: Agent
):
    sub = await create_subscription(
        db_session,
        subscriber_agent_id=stub_agent.id,
        event_type="message.delivered",
        delivery_target="webhook",
        webhook_url="https://example.com/hook",
    )
    await db_session.commit()
    assert sub.delivery_target == "webhook"
    assert sub.webhook_url == "https://example.com/hook"
    assert sub.webhook_secret is not None
    assert sub.webhook_secret.startswith("whsec_")


async def test_subscribe_emit_pull_turn_pass_end_to_end(
    db_session: AsyncSession, stub_agent: Agent
):
    """Item 2(a) — turn.pass round-trips through subscribe → emit →
    pull. Validates that the new event type works as a META-only
    envelope (no body field; small payload)."""
    sub = await create_subscription(
        db_session,
        subscriber_agent_id=stub_agent.id,
        event_type="turn.pass",
        delivery_target="pull",
    )
    await db_session.commit()
    assert sub.event_type == "turn.pass"

    # Emit a META-only turn.pass event (no body coupling — matches
    # the design intent of inbox-watcher recipes).
    await emit_event(
        db_session,
        event_type="turn.pass",
        recipient_agent_id=stub_agent.id,
        payload={"agent_ref": stub_agent.id, "turn_index": 7},
    )
    await db_session.commit()

    events, cursor, has_more = await pull_events(
        db_session,
        recipient_agent_id=stub_agent.id,
        event_type="turn.pass",
    )
    assert len(events) == 1
    assert events[0].event_type == "turn.pass"
    assert events[0].payload == {"agent_ref": stub_agent.id, "turn_index": 7}
    assert has_more is False


async def test_create_subscription_rejects_unknown_event_type(
    db_session: AsyncSession, stub_agent: Agent
):
    with pytest.raises(UnknownEventTypeError):
        await create_subscription(
            db_session,
            subscriber_agent_id=stub_agent.id,
            event_type="not.real.event",
            delivery_target="pull",
        )


async def test_create_subscription_rejects_blocked_webhook_url(
    db_session: AsyncSession, stub_agent: Agent
):
    """SSRF: localhost / private-range URLs are rejected."""
    with pytest.raises(InvalidWebhookUrlError):
        await create_subscription(
            db_session,
            subscriber_agent_id=stub_agent.id,
            event_type="message.delivered",
            delivery_target="webhook",
            webhook_url="http://127.0.0.1:8080/hook",
        )


# ───────────────────────────────────────────────────────────────────────
# list_subscriptions — active-only + scope
# ───────────────────────────────────────────────────────────────────────

async def test_list_subscriptions_returns_active_only(
    db_session: AsyncSession, stub_agent: Agent
):
    sub = await create_subscription(
        db_session,
        subscriber_agent_id=stub_agent.id,
        event_type="message.delivered",
        delivery_target="pull",
    )
    await db_session.commit()
    listing = await list_subscriptions(
        db_session, subscriber_agent_id=stub_agent.id
    )
    assert len(listing) == 1
    assert listing[0].id == sub.id

    # Detach + verify excluded from list.
    detached = await detach_subscription(
        db_session,
        subscription_id=sub.id,
        subscriber_agent_id=stub_agent.id,
    )
    await db_session.commit()
    assert detached is True

    listing_after = await list_subscriptions(
        db_session, subscriber_agent_id=stub_agent.id
    )
    assert listing_after == []


async def test_detach_subscription_idempotent(
    db_session: AsyncSession, stub_agent: Agent
):
    """Second DELETE returns False (no row updated) but does not raise."""
    sub = await create_subscription(
        db_session,
        subscriber_agent_id=stub_agent.id,
        event_type="message.delivered",
        delivery_target="pull",
    )
    await db_session.commit()

    first = await detach_subscription(
        db_session,
        subscription_id=sub.id,
        subscriber_agent_id=stub_agent.id,
    )
    await db_session.commit()
    assert first is True

    second = await detach_subscription(
        db_session,
        subscription_id=sub.id,
        subscriber_agent_id=stub_agent.id,
    )
    await db_session.commit()
    assert second is False


async def test_detach_subscription_wrong_owner_returns_false(
    db_session: AsyncSession, stub_agent: Agent, registered_user: dict
):
    """Detach for a non-owned subscription returns False (no-op)."""
    user_id = await _resolve_user_id(db_session, registered_user["email"])
    other = Agent(
        id="agt_other_own2",
        user_id=user_id,
        slug="other-own",
        display_name="Other Owner",
    )
    db_session.add(other)
    await db_session.commit()

    sub = await create_subscription(
        db_session,
        subscriber_agent_id=stub_agent.id,
        event_type="message.delivered",
        delivery_target="pull",
    )
    await db_session.commit()

    result = await detach_subscription(
        db_session,
        subscription_id=sub.id,
        subscriber_agent_id=other.id,  # not the owner
    )
    await db_session.commit()
    assert result is False

    # Original sub still active.
    listing = await list_subscriptions(
        db_session, subscriber_agent_id=stub_agent.id
    )
    assert len(listing) == 1
