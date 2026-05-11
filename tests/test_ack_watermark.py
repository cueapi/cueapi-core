"""Tests for Item 2(b) — cursor-advance-as-ack semantic.

Resolves Backlog row ``cmp1j1vlp00060`` (CTO concur 2026-05-11).

**Coverage targets**:

- ``advance_ack_watermark`` service helper: pull-mode-only filter,
  event_type filter, monotonicity (never rewinds), no-op safety
- ``ack_subscription`` explicit PATCH path: happy advance,
  monotonic no-rewind, wrong-owner no-op
- ``GET /v1/agents/{ref}/events`` side-effect: cursor advance updates
  matching pull subs' last_acked_event_id
- ``PATCH /v1/agents/{ref}/subscriptions/{id}/ack`` endpoint
- Webhook dispatcher: successful webhook fire advances both
  last_dispatched_event_id AND last_acked_event_id (verified via
  the existing dispatcher integration path)
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.event import Event
from app.models.subscription import Subscription
from app.models.user import User
from app.services.events_service import (
    ack_subscription,
    advance_ack_watermark,
    create_subscription,
    emit_event,
)


async def _resolve_user_id(db_session: AsyncSession, email: str) -> str:
    user = (
        await db_session.execute(select(User).where(User.email == email))
    ).scalar_one()
    return str(user.id)


@pytest_asyncio.fixture
async def ack_agent(db_session: AsyncSession, registered_user: dict) -> Agent:
    user_id = await _resolve_user_id(db_session, registered_user["email"])
    agent = Agent(
        id="agt_acktest00001",
        user_id=user_id,
        slug="ack-test",
        display_name="Ack Watermark Test",
    )
    db_session.add(agent)
    await db_session.commit()
    await db_session.refresh(agent)
    return agent


# ───────────────────────────────────────────────────────────────────────
# advance_ack_watermark — service helper branches
# ───────────────────────────────────────────────────────────────────────


async def test_advance_ack_watermark_updates_pull_subs(
    db_session: AsyncSession, ack_agent: Agent
):
    sub = await create_subscription(
        db_session,
        subscriber_agent_id=ack_agent.id,
        event_type="message.delivered",
        delivery_target="pull",
    )
    await db_session.commit()

    n = await advance_ack_watermark(
        db_session,
        recipient_agent_id=ack_agent.id,
        new_acked_event_id=42,
    )
    await db_session.commit()
    assert n == 1

    refreshed = (
        await db_session.execute(
            select(Subscription).where(Subscription.id == sub.id)
        )
    ).scalar_one()
    assert refreshed.last_acked_event_id == 42


async def test_advance_ack_watermark_skips_webhook_subs(
    db_session: AsyncSession, ack_agent: Agent
):
    """The cursor-advance path ONLY updates pull subs. Webhook subs
    advance via the dispatcher loop (alongside last_dispatched)."""
    webhook_sub = await create_subscription(
        db_session,
        subscriber_agent_id=ack_agent.id,
        event_type="message.delivered",
        delivery_target="webhook",
        webhook_url="https://example.com/hook",
    )
    await db_session.commit()

    n = await advance_ack_watermark(
        db_session,
        recipient_agent_id=ack_agent.id,
        new_acked_event_id=100,
    )
    await db_session.commit()
    # Pull-mode filter excluded the webhook sub.
    assert n == 0

    refreshed = (
        await db_session.execute(
            select(Subscription).where(Subscription.id == webhook_sub.id)
        )
    ).scalar_one()
    assert refreshed.last_acked_event_id is None


async def test_advance_ack_watermark_filters_by_event_type(
    db_session: AsyncSession, ack_agent: Agent
):
    """When event_type is passed, only that type's pull subs advance."""
    md_sub = await create_subscription(
        db_session,
        subscriber_agent_id=ack_agent.id,
        event_type="message.delivered",
        delivery_target="pull",
    )
    tp_sub = await create_subscription(
        db_session,
        subscriber_agent_id=ack_agent.id,
        event_type="turn.pass",
        delivery_target="pull",
    )
    await db_session.commit()

    n = await advance_ack_watermark(
        db_session,
        recipient_agent_id=ack_agent.id,
        new_acked_event_id=50,
        event_type="message.delivered",
    )
    await db_session.commit()
    assert n == 1

    refreshed_md = (
        await db_session.execute(
            select(Subscription).where(Subscription.id == md_sub.id)
        )
    ).scalar_one()
    refreshed_tp = (
        await db_session.execute(
            select(Subscription).where(Subscription.id == tp_sub.id)
        )
    ).scalar_one()
    assert refreshed_md.last_acked_event_id == 50
    assert refreshed_tp.last_acked_event_id is None


async def test_advance_ack_watermark_monotonic_no_rewind(
    db_session: AsyncSession, ack_agent: Agent
):
    """Calling with a lower id doesn't rewind."""
    sub = await create_subscription(
        db_session,
        subscriber_agent_id=ack_agent.id,
        event_type="message.delivered",
        delivery_target="pull",
    )
    await db_session.commit()

    await advance_ack_watermark(
        db_session,
        recipient_agent_id=ack_agent.id,
        new_acked_event_id=100,
    )
    await db_session.commit()

    n = await advance_ack_watermark(
        db_session,
        recipient_agent_id=ack_agent.id,
        new_acked_event_id=50,  # backwards
    )
    await db_session.commit()
    assert n == 0  # no rows updated

    refreshed = (
        await db_session.execute(
            select(Subscription).where(Subscription.id == sub.id)
        )
    ).scalar_one()
    assert refreshed.last_acked_event_id == 100


async def test_advance_ack_watermark_no_matching_subs(
    db_session: AsyncSession, ack_agent: Agent
):
    """No subscriptions for the recipient → returns 0, no error."""
    n = await advance_ack_watermark(
        db_session,
        recipient_agent_id=ack_agent.id,
        new_acked_event_id=10,
    )
    assert n == 0


# ───────────────────────────────────────────────────────────────────────
# ack_subscription — explicit PATCH service path
# ───────────────────────────────────────────────────────────────────────


async def test_ack_subscription_advances_explicit(
    db_session: AsyncSession, ack_agent: Agent
):
    sub = await create_subscription(
        db_session,
        subscriber_agent_id=ack_agent.id,
        event_type="message.delivered",
        delivery_target="webhook",
        webhook_url="https://example.com/hook",
    )
    await db_session.commit()

    ok = await ack_subscription(
        db_session,
        subscription_id=sub.id,
        subscriber_agent_id=ack_agent.id,
        acked_event_id=77,
    )
    await db_session.commit()
    assert ok is True

    refreshed = (
        await db_session.execute(
            select(Subscription).where(Subscription.id == sub.id)
        )
    ).scalar_one()
    assert refreshed.last_acked_event_id == 77


async def test_ack_subscription_no_rewind(
    db_session: AsyncSession, ack_agent: Agent
):
    sub = await create_subscription(
        db_session,
        subscriber_agent_id=ack_agent.id,
        event_type="message.delivered",
        delivery_target="pull",
    )
    await db_session.commit()

    await ack_subscription(
        db_session,
        subscription_id=sub.id,
        subscriber_agent_id=ack_agent.id,
        acked_event_id=100,
    )
    await db_session.commit()

    # Try to ack at a lower value.
    ok = await ack_subscription(
        db_session,
        subscription_id=sub.id,
        subscriber_agent_id=ack_agent.id,
        acked_event_id=50,
    )
    await db_session.commit()
    assert ok is False  # no rows updated

    refreshed = (
        await db_session.execute(
            select(Subscription).where(Subscription.id == sub.id)
        )
    ).scalar_one()
    assert refreshed.last_acked_event_id == 100


async def test_ack_subscription_wrong_owner_no_op(
    db_session: AsyncSession, ack_agent: Agent, registered_user: dict
):
    """Acking another agent's subscription is a silent no-op."""
    user_id = await _resolve_user_id(db_session, registered_user["email"])
    other = Agent(
        id="agt_otherack001",
        user_id=user_id,
        slug="other-ack",
        display_name="Other Ack",
    )
    db_session.add(other)
    await db_session.commit()

    sub = await create_subscription(
        db_session,
        subscriber_agent_id=ack_agent.id,
        event_type="message.delivered",
        delivery_target="pull",
    )
    await db_session.commit()

    ok = await ack_subscription(
        db_session,
        subscription_id=sub.id,
        subscriber_agent_id=other.id,
        acked_event_id=50,
    )
    await db_session.commit()
    assert ok is False


# ───────────────────────────────────────────────────────────────────────
# HTTP integration — GET /events side effect
# ───────────────────────────────────────────────────────────────────────


async def test_pull_events_advances_ack_for_pull_subs(
    client: AsyncClient,
    auth_headers: dict,
    ack_agent: Agent,
    db_session: AsyncSession,
):
    """When GET /events returns events, matching pull subs'
    last_acked_event_id advances to next_cursor."""
    sub = await create_subscription(
        db_session,
        subscriber_agent_id=ack_agent.id,
        event_type="message.delivered",
        delivery_target="pull",
    )
    db_session.add(
        Event(
            event_type="message.delivered",
            recipient_agent_id=ack_agent.id,
            payload={"x": "y"},
            emitted_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    resp = await client.get(
        f"/v1/agents/{ack_agent.id}/events",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["next_cursor"] is not None
    next_cursor = body["next_cursor"]

    # Re-query the sub to see the advanced ack watermark.
    refreshed = (
        await db_session.execute(
            select(Subscription).where(Subscription.id == sub.id)
        )
    ).scalar_one()
    await db_session.refresh(refreshed)
    assert refreshed.last_acked_event_id == next_cursor


async def test_pull_events_no_events_no_ack_advance(
    client: AsyncClient,
    auth_headers: dict,
    ack_agent: Agent,
    db_session: AsyncSession,
):
    """When GET /events returns NO events (next_cursor=None), the
    ack watermark is not touched."""
    sub = await create_subscription(
        db_session,
        subscriber_agent_id=ack_agent.id,
        event_type="message.delivered",
        delivery_target="pull",
    )
    await db_session.commit()

    resp = await client.get(
        f"/v1/agents/{ack_agent.id}/events",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["next_cursor"] is None

    refreshed = (
        await db_session.execute(
            select(Subscription).where(Subscription.id == sub.id)
        )
    ).scalar_one()
    assert refreshed.last_acked_event_id is None


# ───────────────────────────────────────────────────────────────────────
# HTTP integration — PATCH /ack endpoint
# ───────────────────────────────────────────────────────────────────────


async def test_patch_ack_endpoint_advances_watermark(
    client: AsyncClient,
    auth_headers: dict,
    ack_agent: Agent,
    db_session: AsyncSession,
):
    sub = await create_subscription(
        db_session,
        subscriber_agent_id=ack_agent.id,
        event_type="message.delivered",
        delivery_target="webhook",
        webhook_url="https://example.com/hook",
    )
    await db_session.commit()

    resp = await client.patch(
        f"/v1/agents/{ack_agent.id}/subscriptions/{sub.id}/ack",
        json={"acked_event_id": 33},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json() == {"acked": True}

    # Verify via GET /subscriptions surfaces the new value.
    list_resp = await client.get(
        f"/v1/agents/{ack_agent.id}/subscriptions",
        headers=auth_headers,
    )
    entries = list_resp.json()["subscriptions"]
    assert len(entries) == 1
    assert entries[0]["last_acked_event_id"] == 33


async def test_patch_ack_endpoint_rejects_invalid_body(
    client: AsyncClient, auth_headers: dict, ack_agent: Agent
):
    """Body missing acked_event_id → 422. Extra fields → 422 (forbid)."""
    # Need a real subscription_id; use a random UUID — the endpoint
    # validates body BEFORE route resolution, so 422 fires regardless.
    fake_id = "11111111-1111-1111-1111-111111111111"
    resp = await client.patch(
        f"/v1/agents/{ack_agent.id}/subscriptions/{fake_id}/ack",
        json={"wrong_field": 5},
        headers=auth_headers,
    )
    assert resp.status_code == 422


async def test_advance_ack_after_pull_no_cursor_noop(
    db_session: AsyncSession, ack_agent: Agent
):
    """Direct unit test of the route's pure helper. next_cursor=None
    → no-op (no SQL update). Covers the early-return branch for
    ASGI-coverage-tracing reliability."""
    from app.routers.events import _advance_ack_after_pull

    sub = await create_subscription(
        db_session,
        subscriber_agent_id=ack_agent.id,
        event_type="message.delivered",
        delivery_target="pull",
    )
    await db_session.commit()

    # Should not raise; should not advance ack.
    await _advance_ack_after_pull(
        db_session,
        agent_id=ack_agent.id,
        next_cursor=None,
        event_type=None,
    )
    refreshed = (
        await db_session.execute(
            select(Subscription).where(Subscription.id == sub.id)
        )
    ).scalar_one()
    assert refreshed.last_acked_event_id is None


async def test_advance_ack_after_pull_with_cursor_advances(
    db_session: AsyncSession, ack_agent: Agent
):
    """next_cursor provided + matching pull sub → ack advances."""
    from app.routers.events import _advance_ack_after_pull

    sub = await create_subscription(
        db_session,
        subscriber_agent_id=ack_agent.id,
        event_type="message.delivered",
        delivery_target="pull",
    )
    await db_session.commit()

    await _advance_ack_after_pull(
        db_session,
        agent_id=ack_agent.id,
        next_cursor=42,
        event_type=None,
    )
    refreshed = (
        await db_session.execute(
            select(Subscription).where(Subscription.id == sub.id)
        )
    ).scalar_one()
    await db_session.refresh(refreshed)
    assert refreshed.last_acked_event_id == 42


async def test_advance_ack_after_pull_with_event_type_filter(
    db_session: AsyncSession, ack_agent: Agent
):
    """event_type filter passes through to advance_ack_watermark."""
    from app.routers.events import _advance_ack_after_pull

    md_sub = await create_subscription(
        db_session,
        subscriber_agent_id=ack_agent.id,
        event_type="message.delivered",
        delivery_target="pull",
    )
    tp_sub = await create_subscription(
        db_session,
        subscriber_agent_id=ack_agent.id,
        event_type="turn.pass",
        delivery_target="pull",
    )
    await db_session.commit()

    await _advance_ack_after_pull(
        db_session,
        agent_id=ack_agent.id,
        next_cursor=99,
        event_type="message.delivered",
    )

    md_refreshed = (
        await db_session.execute(
            select(Subscription).where(Subscription.id == md_sub.id)
        )
    ).scalar_one()
    tp_refreshed = (
        await db_session.execute(
            select(Subscription).where(Subscription.id == tp_sub.id)
        )
    ).scalar_one()
    await db_session.refresh(md_refreshed)
    await db_session.refresh(tp_refreshed)
    assert md_refreshed.last_acked_event_id == 99
    assert tp_refreshed.last_acked_event_id is None


async def test_advance_ack_after_pull_swallows_db_errors(
    db_session: AsyncSession, ack_agent: Agent
):
    """If advance_ack_watermark raises, the helper rolls back +
    swallows. Pull read already succeeded; ack-advance failure must
    never bubble. Covers the try/except/rollback branches."""
    from unittest.mock import patch as _patch
    from app.routers.events import _advance_ack_after_pull

    with _patch(
        "app.routers.events.advance_ack_watermark",
        side_effect=RuntimeError("simulated DB blip"),
    ):
        # Should NOT raise.
        await _advance_ack_after_pull(
            db_session,
            agent_id=ack_agent.id,
            next_cursor=42,
            event_type=None,
        )
    # Nothing to assert beyond "didn't raise" — the helper's contract
    # is "never propagate the error to the caller."


async def test_patch_ack_idempotent_at_or_past_value(
    client: AsyncClient,
    auth_headers: dict,
    ack_agent: Agent,
    db_session: AsyncSession,
):
    """PATCH with acked_event_id ≤ current watermark is a server-side
    no-op but still returns 200 with {acked: True} — observable end
    state is the same."""
    sub = await create_subscription(
        db_session,
        subscriber_agent_id=ack_agent.id,
        event_type="message.delivered",
        delivery_target="pull",
    )
    await db_session.commit()

    # Advance to 100.
    await client.patch(
        f"/v1/agents/{ack_agent.id}/subscriptions/{sub.id}/ack",
        json={"acked_event_id": 100},
        headers=auth_headers,
    )

    # Re-ack at 50. Server keeps 100.
    resp = await client.patch(
        f"/v1/agents/{ack_agent.id}/subscriptions/{sub.id}/ack",
        json={"acked_event_id": 50},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json() == {"acked": True}

    refreshed = (
        await db_session.execute(
            select(Subscription).where(Subscription.id == sub.id)
        )
    ).scalar_one()
    await db_session.refresh(refreshed)
    assert refreshed.last_acked_event_id == 100  # not rewound
