"""ORM-level schema tests for the messaging primitive (Phase 2.11.1).

Covers the new tables added in migrations 043 + 044:

* ``agents`` — Identity primitive (§2 of MESSAGING_SPEC)
* ``messages`` — Message primitive (§3)
* ``usage_messages_monthly`` — per-user-per-month message counter (§7)

Plus the new columns on existing tables:

* ``users.slug`` — slug-form addressing (§6) + ``monthly_message_limit`` (§7)
* ``dispatch_outbox.execution_id`` / ``cue_id`` now NULLABLE; new
  ``task_type`` values for message delivery (§5).

These tests focus on schema correctness (columns present, constraints
enforced, indexes present). Service-layer tests (Identity router,
Message router, Inbox endpoints, push delivery) come in subsequent
PRs per MESSAGING_SPEC §12.1.2-12.1.5.
"""
from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import Agent, Message, UsageMessagesMonthly, User
from app.utils.ids import (
    generate_agent_id,
    generate_api_key,
    generate_message_id,
    generate_webhook_secret,
    get_api_key_prefix,
    hash_api_key,
)


async def _make_user(db, *, email: str | None = None, slug: str | None = None) -> User:
    """OSS-port note: cueapi-core has no api_keys table (multi-key scoping
    is hosted-only). Original private-monorepo helper inserted an ApiKey
    row alongside the User; here we only insert the User."""
    email = email or f"u-{uuid.uuid4().hex[:8]}@test.com"
    slug = slug or f"user-{uuid.uuid4().hex[:8]}"
    raw_key = generate_api_key()
    user = User(
        email=email,
        api_key_hash=hash_api_key(raw_key),
        api_key_prefix=get_api_key_prefix(raw_key),
        webhook_secret=generate_webhook_secret(),
        slug=slug,
    )
    db.add(user)
    await db.flush()
    return user


async def _make_agent(
    db,
    user: User,
    *,
    slug: str | None = None,
    webhook_url: str | None = None,
    webhook_secret: str | None = None,
    status: str = "online",
    metadata_: dict | None = None,
) -> Agent:
    agent = Agent(
        id=generate_agent_id(),
        user_id=user.id,
        slug=slug or f"agent-{uuid.uuid4().hex[:8]}",
        display_name="Test Agent",
        webhook_url=webhook_url,
        webhook_secret=webhook_secret,
        metadata_=metadata_ or {},
        status=status,
    )
    db.add(agent)
    await db.flush()
    return agent


@pytest.mark.asyncio
async def test_agent_create_minimal(db_session):
    user = await _make_user(db_session)
    agent = await _make_agent(db_session, user, slug="my-agent")
    assert agent.id.startswith("agt_")
    assert agent.user_id == user.id
    assert agent.slug == "my-agent"
    assert agent.display_name == "Test Agent"
    assert agent.status == "online"
    assert agent.webhook_url is None
    assert agent.webhook_secret is None
    assert agent.deleted_at is None
    assert agent.metadata_ == {}


@pytest.mark.asyncio
async def test_agent_slug_unique_per_user(db_session):
    user_a = await _make_user(db_session, slug="user-a")
    user_b = await _make_user(db_session, slug="user-b")
    # Same slug across DIFFERENT users is allowed.
    await _make_agent(db_session, user_a, slug="dock-demo")
    await _make_agent(db_session, user_b, slug="dock-demo")
    await db_session.commit()
    # Same slug TWICE for the same user is rejected.
    with pytest.raises(IntegrityError):
        await _make_agent(db_session, user_a, slug="dock-demo")
        await db_session.commit()


@pytest.mark.asyncio
async def test_agent_status_check_constraint(db_session):
    user = await _make_user(db_session)
    with pytest.raises(IntegrityError):
        await _make_agent(db_session, user, status="invalid_status")
        await db_session.commit()


@pytest.mark.asyncio
async def test_agent_webhook_url_secret_paired(db_session):
    user = await _make_user(db_session)
    # Both NULL — fine (poll-only).
    await _make_agent(db_session, user, slug="poll-only", webhook_url=None, webhook_secret=None)
    # Both set — fine (push-enabled).
    await _make_agent(
        db_session,
        user,
        slug="push-enabled",
        webhook_url="https://example.com/wh",
        webhook_secret=generate_webhook_secret(),
    )
    await db_session.commit()
    # Mismatched (URL set, secret NULL) — rejected.
    with pytest.raises(IntegrityError):
        await _make_agent(
            db_session,
            user,
            slug="mismatched",
            webhook_url="https://example.com/wh",
            webhook_secret=None,
        )
        await db_session.commit()


@pytest.mark.asyncio
async def test_user_slug_unique_globally(db_session):
    await _make_user(db_session, slug="globally-unique")
    await db_session.commit()
    # Same slug across users — rejected (per-tenant unique).
    with pytest.raises(IntegrityError):
        await _make_user(db_session, slug="globally-unique")
        await db_session.commit()


@pytest.mark.asyncio
async def test_user_monthly_message_limit_default(db_session):
    user = await _make_user(db_session)
    await db_session.commit()
    await db_session.refresh(user)
    assert user.monthly_message_limit == 300


def _make_message(
    user: User,
    from_agent: Agent,
    to_agent: Agent,
    *,
    body: str = "hello",
    priority: int = 3,
    delivery_state: str = "queued",
    metadata_: dict | None = None,
    expires_at: dt.datetime | None = None,
) -> Message:
    msg_id = generate_message_id()
    return Message(
        id=msg_id,
        user_id=user.id,
        from_agent_id=from_agent.id,
        to_agent_id=to_agent.id,
        thread_id=msg_id,  # root message: thread_id == self.id
        body=body,
        preview=body[:200],
        priority=priority,
        delivery_state=delivery_state,
        metadata_=metadata_ or {},
        expires_at=expires_at or (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30)),
    )


@pytest.mark.asyncio
async def test_message_create_minimal(db_session):
    user = await _make_user(db_session)
    sender = await _make_agent(db_session, user, slug="sender")
    recipient = await _make_agent(db_session, user, slug="recipient")
    msg = _make_message(user, sender, recipient, body="hello world")
    db_session.add(msg)
    await db_session.commit()
    await db_session.refresh(msg)
    assert msg.id.startswith("msg_")
    assert msg.thread_id == msg.id
    assert msg.priority == 3
    assert msg.delivery_state == "queued"
    assert msg.preview == "hello world"
    assert msg.expects_reply is False
    assert msg.created_at is not None
    assert msg.delivered_at is None


@pytest.mark.asyncio
async def test_message_priority_check_constraint(db_session):
    user = await _make_user(db_session)
    sender = await _make_agent(db_session, user, slug="sender")
    recipient = await _make_agent(db_session, user, slug="recipient")
    for invalid in (0, 6, -1, 100):
        with pytest.raises(IntegrityError):
            db_session.add(_make_message(user, sender, recipient, priority=invalid))
            await db_session.commit()
        await db_session.rollback()


@pytest.mark.asyncio
async def test_message_delivery_state_check_constraint(db_session):
    user = await _make_user(db_session)
    sender = await _make_agent(db_session, user, slug="sender")
    recipient = await _make_agent(db_session, user, slug="recipient")
    valid = ["queued", "delivering", "retry_ready", "delivered", "read", "claimed", "acked", "expired", "failed"]
    for state in valid:
        msg = _make_message(user, sender, recipient, delivery_state=state)
        db_session.add(msg)
    await db_session.commit()
    # Invalid state rejected.
    with pytest.raises(IntegrityError):
        db_session.add(_make_message(user, sender, recipient, delivery_state="bogus"))
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_message_body_size_limit(db_session):
    user = await _make_user(db_session)
    sender = await _make_agent(db_session, user, slug="sender")
    recipient = await _make_agent(db_session, user, slug="recipient")
    # 32 KB exactly — accepted.
    body_max = "a" * 32768
    db_session.add(_make_message(user, sender, recipient, body=body_max))
    await db_session.commit()
    # 32 KB + 1 — rejected.
    with pytest.raises(IntegrityError):
        body_too_big = "a" * 32769
        db_session.add(_make_message(user, sender, recipient, body=body_too_big))
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_usage_messages_monthly_unique_per_user_month(db_session):
    user = await _make_user(db_session)
    today = dt.date.today().replace(day=1)
    db_session.add(
        UsageMessagesMonthly(user_id=user.id, month_start=today, message_count=0)
    )
    await db_session.commit()
    # Same (user_id, month_start) — rejected.
    with pytest.raises(IntegrityError):
        db_session.add(
            UsageMessagesMonthly(user_id=user.id, month_start=today, message_count=10)
        )
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_message_thread_self_root(db_session):
    """Root message has thread_id = self.id; the model construction reflects this."""
    user = await _make_user(db_session)
    sender = await _make_agent(db_session, user, slug="sender")
    recipient = await _make_agent(db_session, user, slug="recipient")
    msg = _make_message(user, sender, recipient, body="root")
    assert msg.thread_id == msg.id
    db_session.add(msg)
    await db_session.commit()


@pytest.mark.asyncio
async def test_message_reply_in_same_thread(db_session):
    """A reply inherits the root's thread_id via reply_to → service layer
    will compute this; the schema doesn't enforce it but allows it."""
    user = await _make_user(db_session)
    sender = await _make_agent(db_session, user, slug="sender")
    recipient = await _make_agent(db_session, user, slug="recipient")
    root = _make_message(user, sender, recipient, body="root")
    db_session.add(root)
    await db_session.flush()

    # Reply: thread_id matches root's id; reply_to references root.
    reply_id = generate_message_id()
    reply = Message(
        id=reply_id,
        user_id=user.id,
        from_agent_id=recipient.id,  # recipient sends back
        to_agent_id=sender.id,
        thread_id=root.id,  # inherits
        reply_to=root.id,
        body="reply",
        preview="reply",
        priority=3,
        delivery_state="queued",
        metadata_={},
        expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30),
    )
    db_session.add(reply)
    await db_session.commit()
    await db_session.refresh(reply)
    assert reply.thread_id == root.id
    assert reply.reply_to == root.id


@pytest.mark.asyncio
async def test_dispatch_outbox_message_task_type_allowed(db_session):
    """New ``task_type`` values for message delivery are accepted."""
    from app.models import DispatchOutbox

    user = await _make_user(db_session)
    sender = await _make_agent(db_session, user, slug="sender")
    recipient = await _make_agent(db_session, user, slug="recipient")
    msg = _make_message(user, sender, recipient, body="for-delivery")
    db_session.add(msg)
    await db_session.flush()

    # Message-task row: execution_id + cue_id NULL; payload has message_id.
    outbox = DispatchOutbox(
        execution_id=None,
        cue_id=None,
        task_type="deliver_message",
        payload={"message_id": msg.id, "to_agent_id": recipient.id},
    )
    db_session.add(outbox)
    await db_session.commit()

    result = await db_session.execute(
        select(DispatchOutbox).where(DispatchOutbox.id == outbox.id)
    )
    row = result.scalar_one()
    assert row.task_type == "deliver_message"
    assert row.execution_id is None
    assert row.payload["message_id"] == msg.id


@pytest.mark.asyncio
async def test_dispatch_outbox_invalid_task_type_rejected(db_session):
    from app.models import DispatchOutbox

    with pytest.raises(IntegrityError):
        db_session.add(
            DispatchOutbox(
                execution_id=uuid.uuid4(),
                cue_id="cue_xxxxxxxxxxxx",
                task_type="garbage",
                payload={},
            )
        )
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_dispatch_outbox_message_task_requires_message_id(db_session):
    """A message-task row without ``payload.message_id`` is rejected."""
    from app.models import DispatchOutbox

    with pytest.raises(IntegrityError):
        db_session.add(
            DispatchOutbox(
                execution_id=None,
                cue_id=None,
                task_type="deliver_message",
                payload={"some_other_key": "value"},  # missing message_id
            )
        )
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_dispatch_outbox_cue_task_still_works(db_session):
    """Existing cue-task rows are still accepted; backward-compat preserved."""
    from datetime import datetime, timezone

    from app.models import DispatchOutbox
    from app.models.execution import Execution
    from tests.test_poller import _create_due_cue, _create_test_user

    # ``execution_id`` is a real FK to ``executions.id`` (migration 002,
    # ON DELETE CASCADE). Anchor the outbox row to a real execution.
    user_id = await _create_test_user(db_session)
    cue = await _create_due_cue(db_session, user_id)
    exec_id = uuid.uuid4()
    db_session.add(
        Execution(
            id=exec_id,
            cue_id=cue.id,
            scheduled_for=datetime.now(timezone.utc),
            status="pending",
        )
    )
    await db_session.commit()

    db_session.add(
        DispatchOutbox(
            execution_id=exec_id,
            cue_id=cue.id,
            task_type="deliver",
            payload={"some": "thing"},
        )
    )
    await db_session.commit()
