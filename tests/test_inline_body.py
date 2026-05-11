"""Tests for Item 1 Option 1 — subscriptions.inline_body opt-in.

CTO concur 2026-05-11 on cardinality + design conclusion. Backlog row
``cmp1j1rzs00020`` resolves when this lands.

**Coverage targets**:

- ``_maybe_embed_body`` pure-helper branches (no sub / inline_body=False
  sub / inline_body=True sub + body ≤ cap / inline_body=True sub +
  body > cap)
- ``emit_event`` end-to-end: body_text=None (existing path unchanged)
  vs body_text provided (new branching)
- ``create_subscription`` accepts inline_body kwarg
- ``SubscriptionCreate`` schema accepts ``inline_body`` field
- ``SubscriptionResponse`` surfaces ``inline_body``
- Mixed-sub: one sub with inline_body=True, one with False — both
  see the SAME embedded event (per-recipient emit, not per-sub)
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
from app.services.events_service import (
    INLINE_BODY_MAX_BYTES,
    _maybe_embed_body,
    create_subscription,
    emit_event,
)


async def _resolve_user_id(db_session: AsyncSession, email: str) -> str:
    user = (
        await db_session.execute(select(User).where(User.email == email))
    ).scalar_one()
    return str(user.id)


@pytest_asyncio.fixture
async def ib_agent(db_session: AsyncSession, registered_user: dict) -> Agent:
    user_id = await _resolve_user_id(db_session, registered_user["email"])
    agent = Agent(
        id="agt_inlinebody01",
        user_id=user_id,
        slug="inline-body",
        display_name="Inline Body Test",
    )
    db_session.add(agent)
    await db_session.commit()
    await db_session.refresh(agent)
    return agent


# ───────────────────────────────────────────────────────────────────────
# _maybe_embed_body — pure helper branches
# ───────────────────────────────────────────────────────────────────────


async def test_maybe_embed_body_no_subscription_returns_unchanged(
    db_session: AsyncSession, ib_agent: Agent
):
    """No subscription for the recipient → body NOT embedded."""
    payload = {"message_id": "msg_x"}
    result = await _maybe_embed_body(
        db_session,
        recipient_agent_id=ib_agent.id,
        event_type="message.delivered",
        body_text="hello world",
        payload=payload,
    )
    assert "body" not in result
    assert result == {"message_id": "msg_x"}


async def test_maybe_embed_body_subscription_inline_false_unchanged(
    db_session: AsyncSession, ib_agent: Agent
):
    """Subscription exists but inline_body=False → body NOT embedded."""
    await create_subscription(
        db_session,
        subscriber_agent_id=ib_agent.id,
        event_type="message.delivered",
        delivery_target="pull",
        inline_body=False,
    )
    await db_session.commit()

    result = await _maybe_embed_body(
        db_session,
        recipient_agent_id=ib_agent.id,
        event_type="message.delivered",
        body_text="hello world",
        payload={"message_id": "msg_x"},
    )
    assert "body" not in result


async def test_maybe_embed_body_inline_true_embeds_small_body(
    db_session: AsyncSession, ib_agent: Agent
):
    """inline_body=True + body ≤ 32KB → body embedded as payload.body."""
    await create_subscription(
        db_session,
        subscriber_agent_id=ib_agent.id,
        event_type="message.delivered",
        delivery_target="pull",
        inline_body=True,
    )
    await db_session.commit()

    result = await _maybe_embed_body(
        db_session,
        recipient_agent_id=ib_agent.id,
        event_type="message.delivered",
        body_text="hello world",
        payload={"message_id": "msg_x"},
    )
    assert result["body"] == "hello world"
    assert "body_omitted" not in result


async def test_maybe_embed_body_inline_true_omits_oversize_body(
    db_session: AsyncSession, ib_agent: Agent
):
    """inline_body=True + body > 32KB → omit-flag + size, no body."""
    await create_subscription(
        db_session,
        subscriber_agent_id=ib_agent.id,
        event_type="message.delivered",
        delivery_target="pull",
        inline_body=True,
    )
    await db_session.commit()

    huge_body = "x" * (INLINE_BODY_MAX_BYTES + 1)
    result = await _maybe_embed_body(
        db_session,
        recipient_agent_id=ib_agent.id,
        event_type="message.delivered",
        body_text=huge_body,
        payload={"message_id": "msg_x"},
    )
    assert "body" not in result
    assert result["body_omitted"] == "size_too_large"
    assert result["body_size_bytes"] == INLINE_BODY_MAX_BYTES + 1


async def test_maybe_embed_body_exactly_at_cap_embeds(
    db_session: AsyncSession, ib_agent: Agent
):
    """Body exactly at 32KB → embedded (≤ cap is inclusive)."""
    await create_subscription(
        db_session,
        subscriber_agent_id=ib_agent.id,
        event_type="message.delivered",
        delivery_target="pull",
        inline_body=True,
    )
    await db_session.commit()

    body_at_cap = "x" * INLINE_BODY_MAX_BYTES
    result = await _maybe_embed_body(
        db_session,
        recipient_agent_id=ib_agent.id,
        event_type="message.delivered",
        body_text=body_at_cap,
        payload={"message_id": "msg_x"},
    )
    assert result["body"] == body_at_cap


async def test_maybe_embed_body_filters_by_event_type(
    db_session: AsyncSession, ib_agent: Agent
):
    """A subscription for ``turn.pass`` with inline_body=True does NOT
    trigger embedding for a ``message.delivered`` emit. Per-event-type
    matching."""
    await create_subscription(
        db_session,
        subscriber_agent_id=ib_agent.id,
        event_type="turn.pass",
        delivery_target="pull",
        inline_body=True,
    )
    await db_session.commit()

    result = await _maybe_embed_body(
        db_session,
        recipient_agent_id=ib_agent.id,
        event_type="message.delivered",
        body_text="hello",
        payload={"message_id": "msg_x"},
    )
    assert "body" not in result


# ───────────────────────────────────────────────────────────────────────
# emit_event integration
# ───────────────────────────────────────────────────────────────────────


async def test_emit_event_no_body_text_skips_subscription_lookup(
    db_session: AsyncSession, ib_agent: Agent
):
    """body_text=None (or omitted) → emit_event proceeds without
    querying subscriptions. Existing v1 behavior preserved."""
    await create_subscription(
        db_session,
        subscriber_agent_id=ib_agent.id,
        event_type="message.delivered",
        delivery_target="pull",
        inline_body=True,
    )
    await db_session.commit()

    event = await emit_event(
        db_session,
        event_type="message.delivered",
        recipient_agent_id=ib_agent.id,
        payload={"message_id": "msg_y"},
        # body_text omitted — emit_event should NOT embed anything
    )
    await db_session.commit()

    # No body fields in the emitted event payload.
    assert "body" not in event.payload
    assert "body_omitted" not in event.payload


async def test_emit_event_with_body_text_and_inline_sub_embeds(
    db_session: AsyncSession, ib_agent: Agent
):
    """body_text=<text> + inline_body=True sub → body in event row."""
    await create_subscription(
        db_session,
        subscriber_agent_id=ib_agent.id,
        event_type="message.delivered",
        delivery_target="pull",
        inline_body=True,
    )
    await db_session.commit()

    event = await emit_event(
        db_session,
        event_type="message.delivered",
        recipient_agent_id=ib_agent.id,
        payload={"message_id": "msg_z"},
        body_text="this is the body",
    )
    await db_session.commit()
    assert event.payload["body"] == "this is the body"
    assert event.payload["message_id"] == "msg_z"


async def test_emit_event_with_body_text_no_inline_sub_skips_embed(
    db_session: AsyncSession, ib_agent: Agent
):
    """body_text provided + only inline_body=False subs → no embed."""
    await create_subscription(
        db_session,
        subscriber_agent_id=ib_agent.id,
        event_type="message.delivered",
        delivery_target="pull",
        inline_body=False,
    )
    await db_session.commit()

    event = await emit_event(
        db_session,
        event_type="message.delivered",
        recipient_agent_id=ib_agent.id,
        payload={"message_id": "msg_a"},
        body_text="body text not embedded",
    )
    await db_session.commit()
    assert "body" not in event.payload


async def test_emit_event_mixed_subs_one_inline_one_not(
    db_session: AsyncSession, ib_agent: Agent
):
    """When ONE sub has inline_body=True (and another has False), the
    event row carries the body. Per-recipient-per-event-type emit
    semantic: single event drives both subs. The False-side sub
    gets a slightly larger payload (acceptable trade-off vs. the
    complexity of per-sub copies)."""
    await create_subscription(
        db_session,
        subscriber_agent_id=ib_agent.id,
        event_type="message.delivered",
        delivery_target="pull",
        inline_body=False,
    )
    await create_subscription(
        db_session,
        subscriber_agent_id=ib_agent.id,
        event_type="message.delivered",
        delivery_target="webhook",
        webhook_url="https://example.com/hook",
        inline_body=True,
    )
    await db_session.commit()

    event = await emit_event(
        db_session,
        event_type="message.delivered",
        recipient_agent_id=ib_agent.id,
        payload={"message_id": "msg_mix"},
        body_text="mixed-sub body",
    )
    await db_session.commit()
    assert event.payload["body"] == "mixed-sub body"


async def test_create_subscription_default_inline_body_is_false(
    db_session: AsyncSession, ib_agent: Agent
):
    """Backward compat: callers that don't pass inline_body get False."""
    sub = await create_subscription(
        db_session,
        subscriber_agent_id=ib_agent.id,
        event_type="message.delivered",
        delivery_target="pull",
    )
    await db_session.commit()
    assert sub.inline_body is False


async def test_create_subscription_inline_body_true_round_trips(
    db_session: AsyncSession, ib_agent: Agent
):
    """inline_body=True at create persists to the row."""
    sub = await create_subscription(
        db_session,
        subscriber_agent_id=ib_agent.id,
        event_type="message.delivered",
        delivery_target="pull",
        inline_body=True,
    )
    await db_session.commit()
    refreshed = (
        await db_session.execute(
            select(Subscription).where(Subscription.id == sub.id)
        )
    ).scalar_one()
    assert refreshed.inline_body is True


# ───────────────────────────────────────────────────────────────────────
# HTTP-route layer surface tests
# ───────────────────────────────────────────────────────────────────────


async def test_post_subscription_accepts_inline_body_field(
    client, auth_headers, ib_agent
):
    """POST /v1/agents/{ref}/subscriptions with inline_body=true round-
    trips through the wire shape."""
    resp = await client.post(
        f"/v1/agents/{ib_agent.id}/subscriptions",
        json={
            "event_type": "message.delivered",
            "delivery_target": "pull",
            "inline_body": True,
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["inline_body"] is True


async def test_post_subscription_default_inline_body_false(
    client, auth_headers, ib_agent
):
    """Omitting inline_body defaults to False on the wire (BackCompat)."""
    resp = await client.post(
        f"/v1/agents/{ib_agent.id}/subscriptions",
        json={"event_type": "message.delivered", "delivery_target": "pull"},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    assert resp.json()["inline_body"] is False
