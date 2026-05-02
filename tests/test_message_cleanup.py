"""Cleanup task tests for the messaging primitive (Phase 2.11.8).

Spec: `https://trydock.ai/mike/cueapi-messaging-primitive-v1-sp` §9 (GDPR retention) +
§13 D7 (30d default TTL) + D10 (7d hard-delete after terminal) +
§8.4 (idempotency-key 24h freeing).

Tests dry-run mode (default; safe to run). Real-deletion is blocked
behind GDPR_CLEANUP_DRY_RUN=false + GDPR_LAST_BACKUP_AT env vars
which are NEVER set in tests — these functions stay in dry-run mode
and only count what would be touched.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from app.models import Agent, Message, User
from app.utils.ids import (
    generate_agent_id,
    generate_api_key,
    generate_message_id,
    generate_webhook_secret,
    get_api_key_prefix,
    hash_api_key,
)
from worker.message_cleanup import (
    cleanup_expired_messages,
    expire_old_messages,
    free_old_idempotency_keys,
)


async def _make_user(db) -> User:
    raw = generate_api_key()
    user = User(
        email=f"u-{uuid.uuid4().hex[:8]}@test.com",
        api_key_hash=hash_api_key(raw),
        api_key_prefix=get_api_key_prefix(raw),
        webhook_secret=generate_webhook_secret(),
        slug=f"u-{uuid.uuid4().hex[:8]}",
    )
    db.add(user)
    await db.flush()
    return user


async def _make_agent(db, user, slug=None) -> Agent:
    agent = Agent(
        id=generate_agent_id(),
        user_id=user.id,
        slug=slug or f"a-{uuid.uuid4().hex[:6]}",
        display_name="Test",
    )
    db.add(agent)
    await db.flush()
    return agent


def _make_message(
    user, sender, recipient, *, body="hi",
    expires_at=None, delivery_state="queued",
    acked_at=None, idempotency_key=None, created_at=None,
) -> Message:
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
        delivery_state=delivery_state,
        idempotency_key=idempotency_key,
        idempotency_fingerprint="x" * 64 if idempotency_key else None,
        created_at=created_at or datetime.now(timezone.utc),
        acked_at=acked_at,
        expires_at=expires_at or (datetime.now(timezone.utc) + timedelta(days=30)),
    )


# ---- expire_old_messages -------------------------------------------------


@pytest.mark.asyncio
async def test_expire_old_messages_dry_run_counts_eligible(db_session):
    user = await _make_user(db_session)
    sender = await _make_agent(db_session, user, slug="exp-s")
    recipient = await _make_agent(db_session, user, slug="exp-r")

    past = datetime.now(timezone.utc) - timedelta(days=1)
    future = datetime.now(timezone.utc) + timedelta(days=10)

    # Eligible: expires_at past, state=queued
    db_session.add(_make_message(user, sender, recipient, body="expired1", expires_at=past))
    # Eligible: expires_at past, state=delivered (not yet terminal)
    db_session.add(_make_message(
        user, sender, recipient, body="expired2",
        expires_at=past, delivery_state="delivered",
    ))
    # NOT eligible: terminal state already
    db_session.add(_make_message(
        user, sender, recipient, body="acked",
        expires_at=past, delivery_state="acked",
    ))
    # NOT eligible: expires_at in future
    db_session.add(_make_message(user, sender, recipient, body="fresh", expires_at=future))
    await db_session.commit()

    result = await expire_old_messages(db_session, dry_run=True)
    assert result["dry_run"] is True
    assert result["eligible_count"] == 2
    assert result["transitioned_count"] == 0  # dry run


# OSS-port note: dropped ``test_expire_old_messages_real_run_refused_without_backup``
# from the private monorepo. That test asserts the GDPR-specific
# safety harness (``GDPR_CLEANUP_DRY_RUN`` + ``GDPR_LAST_BACKUP_AT``
# env vars must be set before real-mode runs). cueapi-core's port
# strips that harness — self-hosters opt in to real action by passing
# ``dry_run=False`` directly.


@pytest.mark.asyncio
async def test_expire_old_messages_no_eligible(db_session):
    user = await _make_user(db_session)
    sender = await _make_agent(db_session, user, slug="no-s")
    recipient = await _make_agent(db_session, user, slug="no-r")
    future = datetime.now(timezone.utc) + timedelta(days=30)
    db_session.add(_make_message(user, sender, recipient, expires_at=future))
    await db_session.commit()

    result = await expire_old_messages(db_session, dry_run=True)
    assert result["eligible_count"] == 0


# ---- cleanup_expired_messages (hard-delete) ------------------------------


@pytest.mark.asyncio
async def test_cleanup_expired_messages_dry_run_counts_terminal(db_session):
    user = await _make_user(db_session)
    sender = await _make_agent(db_session, user, slug="hd-s")
    recipient = await _make_agent(db_session, user, slug="hd-r")

    long_ago = datetime.now(timezone.utc) - timedelta(days=10)
    recent = datetime.now(timezone.utc) - timedelta(days=1)

    # Eligible: acked >7d ago
    db_session.add(_make_message(
        user, sender, recipient, body="old-acked",
        delivery_state="acked", acked_at=long_ago,
    ))
    # Eligible: expired >7d ago (expires_at is the timestamp authority)
    db_session.add(_make_message(
        user, sender, recipient, body="old-expired",
        delivery_state="expired", expires_at=long_ago,
    ))
    # NOT eligible: acked recently (within 7d window)
    db_session.add(_make_message(
        user, sender, recipient, body="recent-acked",
        delivery_state="acked", acked_at=recent,
    ))
    # NOT eligible: not yet terminal
    db_session.add(_make_message(
        user, sender, recipient, body="active",
        delivery_state="delivered",
    ))
    await db_session.commit()

    result = await cleanup_expired_messages(db_session, dry_run=True)
    assert result["dry_run"] is True
    assert result["eligible_count"] == 2
    assert result["deleted_count"] == 0  # dry run


# ---- free_old_idempotency_keys -----------------------------------------


@pytest.mark.asyncio
async def test_free_old_idempotency_keys_dry_run_counts_eligible(db_session):
    user = await _make_user(db_session)
    sender = await _make_agent(db_session, user, slug="ik-s")
    recipient = await _make_agent(db_session, user, slug="ik-r")

    long_ago = datetime.now(timezone.utc) - timedelta(hours=48)
    recent = datetime.now(timezone.utc) - timedelta(hours=1)

    # Eligible: has key, created >24h ago
    db_session.add(_make_message(
        user, sender, recipient, body="old",
        idempotency_key="k-old", created_at=long_ago,
    ))
    # NOT eligible: has key, created <24h ago
    db_session.add(_make_message(
        user, sender, recipient, body="recent",
        idempotency_key="k-recent", created_at=recent,
    ))
    # NOT eligible: no idempotency key
    db_session.add(_make_message(user, sender, recipient, body="no-key"))
    await db_session.commit()

    result = await free_old_idempotency_keys(db_session, dry_run=True)
    assert result["dry_run"] is True
    assert result["eligible_count"] == 1
    assert result["freed_count"] == 0  # dry run
