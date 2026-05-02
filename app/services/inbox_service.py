"""Inbox service — recipient-side message access.

Spec: `https://trydock.ai/mike/cueapi-messaging-primitive-v1-sp` §4 (Inbox + delivery state machine).

The inbox endpoint is THE delivery surface for poll-based agents
(cueapi-Desktop's bundled worker, OpenClaw Gateway in poll mode,
any future agent without a stable HTTP endpoint). Per Mike's
2026-04-30 priority redirection: poll-via-bundled-worker is the v1
universal path; push-via-webhook is a v1.5 optimization.

Key invariant: ``GET /v1/agents/{ref}/inbox`` atomically transitions
``queued`` → ``delivered`` for messages it surfaces. Implemented as a
single ``UPDATE ... RETURNING ...`` so it races cleanly against
concurrent push delivery (which lands in v1.5; today the inbox is the
only delivery path so the transition is simple).

State machine excerpt (§4.1):

```
queued ──poll-fetch (this code path)──> delivered ──read──> read ──ack──> acked
                                            │
                                            └──push-retries-exhausted──> failed
```

Failed messages stay poll-fetchable but DO NOT transition state on
read — failed is intentionally sticky as a sender-facing observability
signal. v1 only sees the queued and read paths since push isn't wired
yet, but the code is forward-compat for v1.5.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthenticatedUser
from app.models import Agent, Message
from app.services.agent_service import get_agent_owned

# Default inbox state filter: anything not yet finalized via ack/expire.
# `failed` is included because the recipient should still see it on poll.
DEFAULT_STATES = ("queued", "delivering", "retry_ready", "delivered", "read", "claimed", "failed")
TERMINAL_STATES = ("acked", "expired")
ALL_STATES = DEFAULT_STATES + TERMINAL_STATES


def _http_error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"error": {"code": code, "message": message, "status": status}},
    )


def _parse_state_filter(
    states: Optional[str], *, default: Tuple[str, ...] = DEFAULT_STATES
) -> Tuple[str, ...]:
    """Parse ``?state=delivered,read`` into a tuple. None → ``default``.

    Validates each value against the allowed set; raises 400 on
    unknown. ``default`` lets callers control the no-filter shape:
    inbox view defaults to non-terminal states (recipient drops acked
    from their default view), sent view defaults to ALL states (sender
    sees their full sent history regardless of recipient lifecycle).
    """
    if states is None:
        return default
    valid = set(ALL_STATES)
    parsed = tuple(s.strip() for s in states.split(",") if s.strip())
    for s in parsed:
        if s not in valid:
            raise _http_error(
                400,
                "invalid_state_filter",
                f"unknown state '{s}'. Valid: {sorted(valid)}",
            )
    return parsed


async def list_inbox(
    db: AsyncSession,
    user: AuthenticatedUser,
    *,
    agent_addr: str,
    states: Optional[str] = None,
    since: Optional[datetime] = None,
    thread_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    count_only: bool = False,
) -> Dict:
    """Return messages addressed TO the given agent (recipient view).

    Side effect: messages currently in ``queued`` state are atomically
    transitioned to ``delivered`` as part of the read query (single
    ``UPDATE ... RETURNING ...``). This implements "poll-fetch counts
    as delivery" per §4.2. The transition is idempotent across
    concurrent fetches.

    `count_only=true` short-circuits to a COUNT query and returns
    `{"count": N}` instead of the message list (R3 dock-demo add).
    """
    agent = await get_agent_owned(db, user, agent_addr, include_deleted=True)
    state_tuple = _parse_state_filter(states)

    base_filters = [
        Message.user_id == user.id,
        Message.to_agent_id == agent.id,
        Message.delivery_state.in_(state_tuple),
    ]
    if since is not None:
        base_filters.append(Message.created_at > since)
    if thread_id is not None:
        base_filters.append(Message.thread_id == thread_id)

    if count_only:
        count_q = select(func.count()).select_from(Message).where(and_(*base_filters))
        count = (await db.execute(count_q)).scalar() or 0
        return {"count": int(count)}

    # Atomic queued→delivered transition: matches msgs that BOTH satisfy
    # the caller's filters AND are currently in queued. RETURNING ids so
    # we can compute the post-transition row count without a follow-up
    # SELECT.
    if "queued" in state_tuple:
        now = datetime.now(timezone.utc)
        upd_q = (
            update(Message)
            .where(
                Message.user_id == user.id,
                Message.to_agent_id == agent.id,
                Message.delivery_state == "queued",
            )
            .values(delivery_state="delivered", delivered_at=now)
            .returning(Message.id)
        )
        await db.execute(upd_q)
        await db.commit()

    # Total (after the transition).
    count_q = select(func.count()).select_from(Message).where(and_(*base_filters))
    total = (await db.execute(count_q)).scalar() or 0

    # Page.
    rows_q = (
        select(Message)
        .where(and_(*base_filters))
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await db.execute(rows_q)).scalars().all()
    return {
        "messages": list(rows),
        "total": int(total),
        "limit": limit,
        "offset": offset,
    }


async def list_sent(
    db: AsyncSession,
    user: AuthenticatedUser,
    *,
    agent_addr: str,
    states: Optional[str] = None,
    since: Optional[datetime] = None,
    thread_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    count_only: bool = False,
) -> Dict:
    """Sender view. No state mutation on read.

    Default state filter is ``ALL_STATES``: a sender should see their
    full sent history regardless of the recipient's lifecycle stage.
    Filtering to a subset still works via the ``?state=`` query param.
    """
    agent = await get_agent_owned(db, user, agent_addr, include_deleted=True)
    state_tuple = _parse_state_filter(states, default=ALL_STATES)

    base_filters = [
        Message.user_id == user.id,
        Message.from_agent_id == agent.id,
        Message.delivery_state.in_(state_tuple),
    ]
    if since is not None:
        base_filters.append(Message.created_at > since)
    if thread_id is not None:
        base_filters.append(Message.thread_id == thread_id)

    if count_only:
        count_q = select(func.count()).select_from(Message).where(and_(*base_filters))
        count = (await db.execute(count_q)).scalar() or 0
        return {"count": int(count)}

    count_q = select(func.count()).select_from(Message).where(and_(*base_filters))
    total = (await db.execute(count_q)).scalar() or 0

    rows_q = (
        select(Message)
        .where(and_(*base_filters))
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await db.execute(rows_q)).scalars().all()
    return {
        "messages": list(rows),
        "total": int(total),
        "limit": limit,
        "offset": offset,
    }
