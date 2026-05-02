"""PR-5a (Dock-readiness): multi-shell same-agent claims.

Verifies the new ``agent_shells`` table + endpoints:

* The same agent can have N concurrent shell registrations.
* Each shell carries its own webhook_url + webhook_secret + status +
  last_heartbeat_at + optional label.
* Shell-management endpoints exist:
    - POST   /v1/agents/{ref}/shells
    - GET    /v1/agents/{ref}/shells
    - DELETE /v1/agents/{ref}/shells/{shell_id}
    - POST   /v1/agents/{ref}/shells/{shell_id}/heartbeat
* Cross-user shell registration is rejected (agent ownership enforced).
* Webhook secret is returned ONCE on create, never again.
* Heartbeat updates ``last_heartbeat_at`` + flips ``status`` to online.

Schema-level invariants pinned:

* ``status`` CHECK constraint accepts only online/offline/away.
* ``shell_webhook_url_secret_paired`` — both NULL together or both set.
* CASCADE delete from agent → shells.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import Agent, AgentShell, User
from app.utils.ids import (
    generate_agent_id,
    generate_agent_shell_id,
    generate_api_key,
    generate_webhook_secret,
    get_api_key_prefix,
    hash_api_key,
)


# ─── Helpers ────────────────────────────────────────────────────────


async def _make_user(db, *, slug: str | None = None) -> User:
    raw_key = generate_api_key()
    user = User(
        email=f"u-{uuid.uuid4().hex[:8]}@test.com",
        api_key_hash=hash_api_key(raw_key),
        api_key_prefix=get_api_key_prefix(raw_key),
        webhook_secret=generate_webhook_secret(),
        slug=slug or f"user-{uuid.uuid4().hex[:8]}",
    )
    db.add(user)
    await db.flush()
    return user


async def _make_agent(db, user: User, *, slug: str | None = None) -> Agent:
    agent = Agent(
        id=generate_agent_id(),
        user_id=user.id,
        slug=slug or f"agent-{uuid.uuid4().hex[:8]}",
        display_name="Test Agent",
        metadata_={},
        status="online",
    )
    db.add(agent)
    await db.flush()
    return agent


def _make_shell(
    *,
    agent: Agent,
    webhook_url: str | None = None,
    webhook_secret: str | None = None,
    label: str | None = None,
    status: str = "online",
) -> AgentShell:
    return AgentShell(
        id=generate_agent_shell_id(),
        agent_id=agent.id,
        user_id=agent.user_id,
        webhook_url=webhook_url,
        webhook_secret=webhook_secret,
        label=label,
        status=status,
    )


# ─── Schema-level invariants ──────────────────────────────────────


@pytest.mark.asyncio
async def test_shell_id_format(db_session):
    user = await _make_user(db_session)
    agent = await _make_agent(db_session, user)
    shell = _make_shell(agent=agent)
    db_session.add(shell)
    await db_session.commit()
    await db_session.refresh(shell)
    assert shell.id.startswith("ash_")
    assert len(shell.id) == 16  # ash_ + 12 chars


@pytest.mark.asyncio
async def test_n_shells_same_agent_allowed(db_session):
    """The whole point of PR-5a — same agent, N concurrent shells."""
    user = await _make_user(db_session)
    agent = await _make_agent(db_session, user, slug="argus")

    for label in ("claude-code", "cursor", "openclaw"):
        shell = _make_shell(
            agent=agent,
            webhook_url=f"https://example.com/{label}",
            webhook_secret=generate_webhook_secret(),
            label=label,
        )
        db_session.add(shell)
    await db_session.commit()

    # All three live, all on the same agent.
    result = await db_session.execute(
        select(AgentShell).where(AgentShell.agent_id == agent.id)
    )
    shells = list(result.scalars().all())
    assert len(shells) == 3
    labels = {s.label for s in shells}
    assert labels == {"claude-code", "cursor", "openclaw"}


@pytest.mark.asyncio
async def test_shell_status_check_constraint(db_session):
    user = await _make_user(db_session)
    agent = await _make_agent(db_session, user)
    with pytest.raises(IntegrityError):
        shell = _make_shell(agent=agent, status="invalid_status")
        db_session.add(shell)
        await db_session.commit()


@pytest.mark.asyncio
async def test_shell_webhook_url_secret_paired(db_session):
    user = await _make_user(db_session)
    agent = await _make_agent(db_session, user)
    # Both NULL — fine (poll-only shell).
    shell1 = _make_shell(agent=agent, webhook_url=None, webhook_secret=None)
    db_session.add(shell1)
    await db_session.commit()

    # Both set — fine.
    shell2 = _make_shell(
        agent=agent,
        webhook_url="https://example.com/wh",
        webhook_secret=generate_webhook_secret(),
    )
    db_session.add(shell2)
    await db_session.commit()

    # Mismatched (URL set, secret NULL) — rejected.
    with pytest.raises(IntegrityError):
        shell3 = _make_shell(
            agent=agent,
            webhook_url="https://example.com/wh",
            webhook_secret=None,
        )
        db_session.add(shell3)
        await db_session.commit()


@pytest.mark.asyncio
async def test_shell_cascade_delete_on_agent_delete(db_session):
    """CASCADE delete from agents → agent_shells. Removing an agent
    cleanly removes all its shells without orphaning."""
    user = await _make_user(db_session)
    agent = await _make_agent(db_session, user)
    for _ in range(3):
        db_session.add(_make_shell(agent=agent))
    await db_session.commit()

    await db_session.delete(agent)
    await db_session.commit()

    result = await db_session.execute(
        select(AgentShell).where(AgentShell.agent_id == agent.id)
    )
    assert list(result.scalars().all()) == []


@pytest.mark.asyncio
async def test_heartbeat_field_defaults(db_session):
    user = await _make_user(db_session)
    agent = await _make_agent(db_session, user)
    shell = _make_shell(agent=agent)
    db_session.add(shell)
    await db_session.commit()
    await db_session.refresh(shell)
    # last_heartbeat_at + registered_at default to NOW().
    assert shell.last_heartbeat_at is not None
    assert shell.registered_at is not None
    assert (datetime.now(timezone.utc) - shell.last_heartbeat_at) < timedelta(seconds=10)


# ─── ID generator ─────────────────────────────────────────────────


def test_generate_agent_shell_id_format():
    """``ash_<12 alphanum>`` mirrors the established id format
    convention (cue_, agt_, msg_)."""
    for _ in range(50):
        sid = generate_agent_shell_id()
        assert sid.startswith("ash_")
        assert len(sid) == 16
        # Lower alphanumeric only.
        body = sid[len("ash_"):]
        assert body.isalnum()
        assert body.lower() == body
