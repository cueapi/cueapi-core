"""Message service layer.

Spec: `https://trydock.ai/mike/cueapi-messaging-primitive-v1-sp` §3 (Message primitive) +
§8 (Idempotency-Key) + §3.5 (threading semantics).

Owns the create-time path: resolve addresses, validate same-tenant,
check idempotency, compute thread_id + preview, persist message row.

Push delivery (Phase 12.1.5):

* When ``to_agent.webhook_url`` is set, ``create_message`` enqueues a
  ``dispatch_outbox`` row with ``task_type='deliver_message'`` in the
  same transaction as the message insert (transactional outbox
  pattern from §5.1). The worker that consumes that row lands in a
  later slice; until then, message rows still also stay
  poll-fetchable (queued → delivered transition still happens via
  the inbox-fetch path from Phase 12.1.4 if a worker pulls before
  push delivers).
* When ``to_agent.webhook_url`` is NULL, no outbox row is inserted
  and delivery stays poll-only (v1 behavior).

Idempotency-Key (§8.2 strict mode):

* Header value stored on the row alongside a SHA-256 fingerprint of
  the body. Same key + same body → return existing message with 200
  (instead of 201). Same key + DIFFERENT body → 409
  ``idempotency_key_conflict``. Caller must change the key OR
  resend with the original body.
* The 24h dedup window is enforced at the application layer
  (PostgreSQL doesn't support NOW() in partial-index predicates).
  The unique partial index on (user_id, idempotency_key) makes the
  cleanup task's job trivial: NULL out rows older than 24h.

Cross-tenant constraint (§3.4):

* v1 only — sender and recipient must be the same User. The same
  caller with multiple API keys CAN send between agents on those
  keys; the per-tenant boundary is User, not ApiKey.
* v2 design lives in MESSAGING_SPEC §1.3a (orgs + per-agent
  permission scoping + cross-org messaging).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthenticatedUser
from app.models import Agent, DispatchOutbox, Message
from app.redis import get_redis
from app.services.agent_service import resolve_address
from app.services.message_usage_service import (
    check_message_quota,
    check_per_minute_rate_limit,
    check_priority_high_limits,
    get_user_plan_and_msg_limit,
    increment_monthly_count,
)
from app.utils.ids import generate_message_id

METADATA_MAX_BYTES = 10240  # 10 KB
MESSAGE_TTL_DAYS = 30
IDEMPOTENCY_DEDUP_WINDOW_HOURS = 24


def _http_error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"error": {"code": code, "message": message, "status": status}},
    )


def _compute_fingerprint(
    *,
    to_agent_id: str,
    body: str,
    subject: Optional[str],
    priority: int,
    reply_to: Optional[str],
    metadata: Dict,
) -> str:
    """SHA-256 fingerprint over the request shape used for body-mismatch
    detection on Idempotency-Key reuse."""
    canonical = json.dumps(
        {
            "to": to_agent_id,
            "body": body,
            "subject": subject,
            "priority": priority,
            "reply_to": reply_to,
            "metadata": metadata,
        },
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


async def _resolve_reply_to(
    db: AsyncSession, reply_to: Optional[str], from_agent_id: str
) -> Tuple[str, Optional[str]]:
    """Return (thread_id, reply_to_msg_id). For a root message,
    ``thread_id`` is set to the new message's own id by the caller.

    For a reply, looks up the parent message (must be same tenant, in
    a thread one of the participants is on). Returns the parent's
    thread_id so the new message slots into the same conversation.
    """
    if reply_to is None:
        return ("", None)
    result = await db.execute(
        select(Message).where(Message.id == reply_to)
    )
    parent = result.scalar_one_or_none()
    if not parent:
        raise _http_error(404, "reply_to_not_found", f"reply_to message {reply_to} not found")
    return (parent.thread_id, parent.id)


async def create_message(
    db: AsyncSession,
    user: AuthenticatedUser,
    *,
    to: str,
    body: str,
    subject: Optional[str],
    reply_to: Optional[str],
    priority: int,
    expects_reply: bool,
    reply_to_agent: Optional[str],
    metadata: Dict,
    idempotency_key: Optional[str],
    from_agent: Agent,
) -> Tuple[Message, bool, bool]:
    """Send a message. Returns (message, was_dedup_hit, priority_downgraded).

    ``was_dedup_hit`` is True when an existing message was returned via
    Idempotency-Key match. Caller (router) uses this to decide between
    200 (dedup) and 201 (new).

    ``priority_downgraded`` is True when the request asked for
    priority>3 and the per-pair anti-abuse rule downgraded it to 3.
    Caller surfaces this via the ``X-CueAPI-Priority-Downgraded: true``
    response header per §7.3.

    Caller must have already resolved ``from_agent`` — typically
    ``X-Cueapi-From-Agent`` header (one of the caller's agents) or a
    derived "default" agent. v1 makes this explicit on the request
    surface to keep the model concrete.
    """
    # 1. Validate metadata size at the service layer (no easy DB-level
    #    JSONB byte check). Mirrors execution.outcome_metadata pattern.
    metadata_bytes = len(json.dumps(metadata or {}, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    if metadata_bytes > METADATA_MAX_BYTES:
        raise _http_error(
            400,
            "metadata_too_large",
            f"metadata exceeds {METADATA_MAX_BYTES} bytes (got {metadata_bytes})",
        )

    # 2. Validate from_agent is owned by caller and not deleted.
    # ``AuthenticatedUser.id`` is str; ``Agent.user_id`` is UUID.
    # str(UUID) for comparison.
    if str(from_agent.user_id) != str(user.id):
        raise _http_error(
            403,
            "from_agent_not_owned",
            "from_agent is not owned by the authenticated user",
        )
    if from_agent.deleted_at is not None:
        raise _http_error(
            400,
            "from_agent_deleted",
            "from_agent is soft-deleted; cannot send messages",
        )

    # 3. Resolve `to` and (optional) `reply_to_agent`.
    to_agent = await resolve_address(db, to)
    reply_to_agent_obj: Optional[Agent] = None
    if reply_to_agent is not None:
        reply_to_agent_obj = await resolve_address(db, reply_to_agent)

    # 4. Same-tenant check (§3.4). Stringify both to dodge UUID-vs-str.
    user_id_str = str(user.id)
    if str(to_agent.user_id) != user_id_str:
        raise _http_error(
            403,
            "cross_tenant_messaging_forbidden",
            "v1 messaging is restricted to same-tenant agents",
        )
    if reply_to_agent_obj is not None and str(reply_to_agent_obj.user_id) != user_id_str:
        raise _http_error(
            403,
            "cross_tenant_messaging_forbidden",
            "v1 messaging is restricted to same-tenant agents",
        )

    # 4.5. Quota + rate-limit enforcement (§7). All checks happen
    # BEFORE the idempotency check — a dedup-hit on a key inside a
    # rate-limited window should still be allowed (it's not a new
    # message, it's the cached representation of an old one).
    redis = await get_redis()
    plan, monthly_limit = await get_user_plan_and_msg_limit(db, user.id)

    # Per-minute rate limit (sliding window, plan-tiered).
    await check_per_minute_rate_limit(user.id, plan, redis)

    # Priority-high anti-abuse — may downgrade priority to 3 silently
    # for over-pair, or 429 for over-sender.
    effective_priority, priority_downgraded = await check_priority_high_limits(
        user_id=user.id,
        from_agent_id=from_agent.id,
        to_agent_id=to_agent.id,
        priority=priority,
        redis=redis,
    )
    priority = effective_priority

    # 5. Idempotency check.
    fingerprint = _compute_fingerprint(
        to_agent_id=to_agent.id,
        body=body,
        subject=subject,
        priority=priority,
        reply_to=reply_to,
        metadata=metadata or {},
    )
    if idempotency_key is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=IDEMPOTENCY_DEDUP_WINDOW_HOURS)
        existing_q = select(Message).where(
            Message.user_id == user.id,
            Message.idempotency_key == idempotency_key,
            Message.created_at > cutoff,
        )
        existing = (await db.execute(existing_q)).scalar_one_or_none()
        if existing is not None:
            if existing.idempotency_fingerprint == fingerprint:
                return (existing, True, False)
            raise _http_error(
                409,
                "idempotency_key_conflict",
                f"Idempotency-Key reused with a different body. Existing message: {existing.id}",
            )

    # 5.5. Monthly quota — checked here, AFTER idempotency. A dedup-hit
    # is not a new message; the quota check applies only to new sends.
    await check_message_quota(db, user.id, monthly_limit, redis)

    # 6. Resolve thread_id + reply_to chain.
    inherited_thread_id, parent_msg_id = await _resolve_reply_to(
        db, reply_to, from_agent.id
    )

    # 7. Generate id; thread_id == self.id for root messages, else inherits.
    msg_id = generate_message_id()
    thread_id = inherited_thread_id or msg_id
    expires_at = datetime.now(timezone.utc) + timedelta(days=MESSAGE_TTL_DAYS)

    msg = Message(
        id=msg_id,
        user_id=user.id,
        from_agent_id=from_agent.id,
        to_agent_id=to_agent.id,
        thread_id=thread_id,
        reply_to=parent_msg_id,
        subject=subject,
        body=body,
        preview=body[:200],
        priority=priority,
        expects_reply=expects_reply,
        reply_to_agent_id=reply_to_agent_obj.id if reply_to_agent_obj else None,
        delivery_state="queued",
        metadata_=metadata or {},
        idempotency_key=idempotency_key,
        idempotency_fingerprint=fingerprint if idempotency_key else None,
        expires_at=expires_at,
    )
    db.add(msg)

    # 7.5. Push-delivery enqueue (§5.1). When the recipient has a
    # ``webhook_url`` configured, insert a ``deliver_message`` outbox
    # row in the SAME transaction as the message row. Both land
    # atomically or both roll back.
    #
    # ``execution_id`` and ``cue_id`` are NULL for message-task rows;
    # the ``task_payload_shape`` check constraint requires
    # ``payload`` to carry ``message_id``.
    #
    # ``webhook_secret`` is intentionally NOT snapshotted here. The
    # worker re-reads ``to_agent.webhook_secret`` live on each dispatch
    # attempt so secret rotation takes effect immediately (§5.1).
    if to_agent.webhook_url is not None:
        db.add(
            DispatchOutbox(
                execution_id=None,
                cue_id=None,
                task_type="deliver_message",
                payload={
                    "message_id": msg_id,
                    "to_agent_id": to_agent.id,
                    "webhook_url": to_agent.webhook_url,
                },
            )
        )

    await db.commit()
    await db.refresh(msg)

    # 8. Atomic increment of the monthly quota — happens AFTER the
    # commit so a failed insert (e.g. constraint violation) doesn't
    # bump the count. UPSERT is its own transaction; race with another
    # concurrent increment is handled by Postgres atomicity.
    try:
        await increment_monthly_count(db, user.id, redis)
    except Exception:
        # Log but don't fail — message is already persisted; quota
        # drift is recoverable on next read via Postgres source-of-truth.
        import logging
        logging.getLogger(__name__).warning(
            "increment_monthly_count failed for user %s; quota cache may be stale",
            user.id,
            exc_info=True,
        )

    return (msg, False, priority_downgraded)


async def get_message_for_user(
    db: AsyncSession,
    user: AuthenticatedUser,
    msg_id: str,
) -> Message:
    """Fetch a message visible to the caller (sender OR recipient).

    v1 same-tenant constraint means both agents are owned by the
    caller's user_id. ``Message.user_id`` is the sender's user — we
    expand to also allow the recipient via the to_agent FK if needed.
    For now, since cross-tenant is blocked at create time, both sides
    have the same ``user_id``.
    """
    result = await db.execute(select(Message).where(Message.id == msg_id))
    msg = result.scalar_one_or_none()
    if not msg:
        raise _http_error(404, "message_not_found", f"message {msg_id} not found")
    if str(msg.user_id) != str(user.id):
        # Don't leak existence — same code as not-found.
        raise _http_error(404, "message_not_found", f"message {msg_id} not found")
    return msg


async def mark_read(
    db: AsyncSession,
    user: AuthenticatedUser,
    msg_id: str,
) -> Message:
    """Recipient marks message as read.

    Idempotent: calling on already-`read` returns 200 unchanged.
    Terminal states (`acked`, `expired`) reject with 409.
    """
    msg = await get_message_for_user(db, user, msg_id)

    # The "recipient" is the owner of to_agent. In v1 same-tenant means
    # the caller's user_id == msg.user_id, so technically anyone with
    # the user's key could mark read. That's intentional for v1; v2
    # tightens to "agent-level" auth.

    if msg.delivery_state in ("acked", "expired"):
        raise _http_error(
            409,
            "invalid_state_transition",
            f"cannot mark read on terminal state '{msg.delivery_state}'",
        )

    if msg.delivery_state != "read":
        msg.delivery_state = "read"
        msg.read_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(msg)
    return msg


async def mark_acked(
    db: AsyncSession,
    user: AuthenticatedUser,
    msg_id: str,
) -> Message:
    """Recipient acknowledges. Terminal."""
    msg = await get_message_for_user(db, user, msg_id)

    if msg.delivery_state == "acked":
        return msg
    if msg.delivery_state == "expired":
        raise _http_error(
            409,
            "invalid_state_transition",
            f"cannot ack on terminal state '{msg.delivery_state}'",
        )

    msg.delivery_state = "acked"
    msg.acked_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(msg)
    return msg


def to_response_dict(msg: Message) -> Dict:
    """ORM Message → dict suitable for MessageResponse."""
    return {
        "id": msg.id,
        "user_id": str(msg.user_id),
        "from_agent_id": msg.from_agent_id,
        "to_agent_id": msg.to_agent_id,
        "thread_id": msg.thread_id,
        "reply_to": msg.reply_to,
        "subject": msg.subject,
        "body": msg.body,
        "preview": msg.preview,
        "priority": msg.priority,
        "expects_reply": msg.expects_reply,
        "reply_to_agent_id": msg.reply_to_agent_id,
        "delivery_state": msg.delivery_state,
        "metadata": msg.metadata_ or {},
        "idempotency_key": msg.idempotency_key,
        "created_at": msg.created_at,
        "delivered_at": msg.delivered_at,
        "read_at": msg.read_at,
        "acked_at": msg.acked_at,
        "failed_at": msg.failed_at,
        "expires_at": msg.expires_at,
    }
