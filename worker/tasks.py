from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import redis.asyncio as aioredis

from app.config import settings
from app.models.agent import Agent
from app.models.cue import Cue
from app.models.dispatch_outbox import DispatchOutbox
from app.models.execution import Execution
from app.models.message import Message
from app.models.user import User
from app.services.message_classification import (
    EVT_4XX_TERMINAL,
    EVT_RETRIES_EXHAUSTED,
    EVT_RETRY_SCHEDULED,
    EVT_429_RETRY_AFTER,
)
from app.services.message_delivery import (
    DeliveryAttemptResult,
    deliver_message_to_webhook,
)
from app.services.usage_service import check_execution_limit, increment_usage
from app.services.webhook import deliver_webhook
from app.utils.retry_after import parse_retry_after

# ── Messaging primitive push delivery constants (Phase 12.1.5) ─────
#
# ``MESSAGE_RETRY_MAX_ATTEMPTS`` = 3 retries AFTER the initial delivery
# = 4 total attempts. Matches cue convention.
MESSAGE_RETRY_MAX_ATTEMPTS = 3
MESSAGE_RETRY_BACKOFF_MINUTES = [1, 5, 15]

logger = logging.getLogger(__name__)


async def _send_failure_email(
    session: AsyncSession,
    user_id: str,
    cue_id: str,
    cue_name: str,
    execution_id: str,
    error_message: str,
):
    """Send email notification when execution fails after all retries.

    Rate limited: max 10 failure emails per hour per user via Redis counter.
    Suppressed for test/ephemeral cues.
    """
    from app.services.email_service import is_test_cue
    if is_test_cue(cue_name):
        logger.info("Failure email suppressed for test cue: %s", cue_name)
        return

    try:
        redis_client = aioredis.from_url(settings.REDIS_URL)
        rate_key = f"failure_email:{user_id}"
        count = await redis_client.incr(rate_key)
        if count == 1:
            await redis_client.expire(rate_key, 3600)
        if count > 10:
            logger.info("Failure email rate limited", extra={
                "user_id": user_id, "cue_id": cue_id, "count": count,
            })
            await redis_client.aclose()
            return

        # Fetch user email
        result = await session.execute(
            select(User.email).where(User.id == user_id)
        )
        row = result.fetchone()
        if not row or not row.email:
            await redis_client.aclose()
            return

        if settings.RESEND_API_KEY:
            import resend
            from app.utils.templates import brand_email, email_button, email_code, email_paragraph

            resend.api_key = settings.RESEND_API_KEY
            body_html = (
                email_paragraph(
                    f"Your cue <strong style='color:#ffffff;'>{cue_name}</strong> "
                    f"({email_code(cue_id)}) failed after all retries were exhausted."
                )
                + email_paragraph(
                    f"<strong style='color:#ffffff;'>Execution ID:</strong> {email_code(execution_id)}"
                )
                + email_paragraph(
                    f"<strong style='color:#ffffff;'>Error:</strong> {error_message[:500]}"
                )
                + f'<p style="margin:24px 0;">{email_button("View Dashboard", "https://dashboard.cueapi.ai")}</p>'
            )

            # Try sending with one retry on failure
            last_exc = None
            for attempt in range(2):
                try:
                    resend.Emails.send({
                        "from": settings.RESEND_FROM_EMAIL or "CueAPI <noreply@cueapi.ai>",
                        "to": [row.email],
                        "subject": f"[CueAPI] Execution failed: {cue_name}",
                        "html": brand_email(f"Execution Failed: {cue_name}", body_html),
                    })
                    logger.info("Failure email sent", extra={
                        "user_id": user_id, "cue_id": cue_id, "execution_id": execution_id,
                    })
                    last_exc = None
                    break
                except Exception as e:
                    last_exc = e
                    if attempt == 0:
                        logger.warning(f"Failure email send failed, retrying in 5s: {e}")
                        await asyncio.sleep(5)
            if last_exc:
                logger.error(f"Failed to send failure email after 2 attempts: {last_exc}", extra={
                    "user_id": user_id, "cue_id": cue_id, "execution_id": execution_id,
                })
        else:
            logger.error(
                "RESEND_API_KEY not configured — cannot send failure email",
                extra={"user_id": user_id, "cue_id": cue_id, "execution_id": execution_id},
            )

        await redis_client.aclose()
    except Exception as e:
        logger.error(f"Failed to send failure email: {e}", extra={
            "user_id": user_id, "cue_id": cue_id,
        })


async def _send_failure_webhook(
    webhook_url: str,
    cue_id: str,
    cue_name: str,
    attempts: int,
    last_http_status: Optional[int],
    last_error: str,
    failed_at: datetime,
):
    """POST failure details to the on_failure.webhook URL."""
    import httpx

    payload = {
        "event": "cue.failed",
        "cue_id": cue_id,
        "cue_name": cue_name,
        "attempts": attempts,
        "last_http_status": last_http_status,
        "last_error": last_error[:500],
        "failed_at": failed_at.isoformat(),
        "dashboard_url": "https://dashboard.cueapi.ai",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                webhook_url,
                json=payload,
                headers={"Content-Type": "application/json", "User-Agent": "CueAPI/1.0"},
            )
            logger.info(
                "Failure webhook sent: url=%s, status=%d, cue_id=%s",
                webhook_url, resp.status_code, cue_id,
            )
    except Exception as e:
        logger.warning(f"Failed to send failure webhook to {webhook_url}: {e}")


async def _get_db_session(ctx: dict) -> AsyncSession:
    """Get a DB session from the worker context."""
    session_factory = ctx.get("db_session_factory")
    if session_factory is None:
        raise RuntimeError("db_session_factory not found in worker context")
    return session_factory()


async def _claim_execution(session: AsyncSession, execution_id: str, expected_status: str) -> bool:
    """Conditional UPDATE claim. Returns True if this worker won the claim.

    Uses a single expected_status (not a list) to prevent duplicate claims:
    - Initial delivery claims from 'pending' only
    - Retry delivery claims from 'retry_ready' only

    started_at is only set on the first claim (when it's NULL), preserving
    the timestamp of the original delivery attempt across retries.
    """
    result = await session.execute(
        update(Execution)
        .where(
            Execution.id == execution_id,
            Execution.status == expected_status,
        )
        .values(
            status="delivering",
            # Only set started_at on first claim (preserve across retries)
            started_at=text("CASE WHEN started_at IS NULL THEN now() ELSE started_at END"),
            updated_at=datetime.now(timezone.utc),
        )
        .returning(Execution.id)
    )
    row = result.fetchone()
    await session.commit()
    return row is not None


async def _handle_success(
    session: AsyncSession,
    execution_id: str,
    http_status: int,
    response_body: str,
    attempt: int,
    cue_id: str,
    user_id: Optional[str] = None,
    redis_client: Optional[aioredis.Redis] = None,
):
    """Mark execution as success and update cue."""
    now = datetime.now(timezone.utc)

    await session.execute(
        update(Execution)
        .where(Execution.id == execution_id)
        .values(
            status="success",
            http_status=http_status,
            response_body=response_body,
            delivered_at=now,
            last_attempt_at=now,
            attempts=attempt,
            next_retry=None,  # Clear on success
            updated_at=now,
        )
    )

    # Update cue: last_run (run_count incremented on every attempt, not just success)
    await session.execute(
        update(Cue)
        .where(Cue.id == cue_id)
        .values(
            last_run=now,
            updated_at=now,
        )
    )

    # Check if one-time cue → mark completed
    cue_result = await session.execute(
        select(Cue.schedule_type).where(Cue.id == cue_id)
    )
    cue_row = cue_result.fetchone()
    if cue_row and cue_row.schedule_type == "once":
        await session.execute(
            update(Cue)
            .where(Cue.id == cue_id)
            .values(status="completed", updated_at=now)
        )

    await session.commit()

    # Increment usage tracking
    if user_id and redis_client:
        try:
            await increment_usage(user_id, redis_client, session)
        except Exception as e:
            logger.warning(f"Failed to increment usage for user {user_id}: {e}")


async def _handle_failure(
    session: AsyncSession,
    execution_id: str,
    http_status: Optional[int],
    error_message: str,
    attempt: int,
    cue_id: str,
    retry_max_attempts: int,
    retry_backoff_minutes: list,
):
    """Mark execution as retrying or failed."""
    now = datetime.now(timezone.utc)

    if attempt < retry_max_attempts:
        # Calculate next retry
        backoff_idx = min(attempt - 1, len(retry_backoff_minutes) - 1)
        backoff_minutes = retry_backoff_minutes[backoff_idx] if retry_backoff_minutes else 1
        next_retry = now + timedelta(minutes=backoff_minutes)

        await session.execute(
            update(Execution)
            .where(Execution.id == execution_id)
            .values(
                status="retrying",
                http_status=http_status,
                error_message=error_message,
                last_attempt_at=now,
                attempts=attempt,
                next_retry=next_retry,
                updated_at=now,
            )
        )
    else:
        # All retries exhausted
        await session.execute(
            update(Execution)
            .where(Execution.id == execution_id)
            .values(
                status="failed",
                http_status=http_status,
                error_message=error_message,
                last_attempt_at=now,
                attempts=attempt,
                next_retry=None,
                updated_at=now,
            )
        )

        # Update cue last_run
        await session.execute(
            update(Cue)
            .where(Cue.id == cue_id)
            .values(last_run=now, updated_at=now)
        )

        # Fetch cue details for escalation
        cue_result = await session.execute(
            select(
                Cue.schedule_type, Cue.name, Cue.user_id, Cue.on_failure
            ).where(Cue.id == cue_id)
        )
        cue_row = cue_result.fetchone()

        # Determine on_failure config (default: email=True, webhook=None, pause=False)
        on_failure = (cue_row.on_failure if cue_row and cue_row.on_failure else {}) if cue_row else {}
        on_failure_email = on_failure.get("email", True)
        on_failure_webhook = on_failure.get("webhook")
        on_failure_pause = on_failure.get("pause", False)

        # If one-time cue → mark failed
        if cue_row and cue_row.schedule_type == "once":
            await session.execute(
                update(Cue)
                .where(Cue.id == cue_id)
                .values(status="failed", updated_at=now)
            )

        # on_failure.pause: pause the cue after final failure
        if on_failure_pause and cue_row and cue_row.schedule_type != "once":
            await session.execute(
                update(Cue)
                .where(Cue.id == cue_id)
                .values(status="paused", next_run=None, updated_at=now)
            )

        await session.commit()

        # on_failure.email: send failure notification email (rate limited)
        if on_failure_email and cue_row:
            await _send_failure_email(
                session, cue_row.user_id, cue_id,
                cue_row.name or cue_id, execution_id, error_message,
            )

        # on_failure.webhook: POST failure details to escalation URL
        if on_failure_webhook and cue_row:
            await _send_failure_webhook(
                on_failure_webhook, cue_id,
                cue_row.name or cue_id, attempt,
                http_status, error_message, now,
            )

        return

    await session.commit()


async def _get_redis(ctx: dict) -> Optional[aioredis.Redis]:
    """Get Redis client from worker context."""
    return ctx.get("redis")


async def deliver_webhook_task(ctx: dict, payload: dict):
    """arq task: deliver a webhook for a new execution."""
    session = await _get_db_session(ctx)
    redis_client = await _get_redis(ctx)
    try:
        execution_id = payload["execution_id"]
        cue_id = payload["cue_id"]
        user_id = payload.get("user_id")

        # Check execution limit before delivering
        if user_id and redis_client:
            monthly_limit = payload.get("monthly_execution_limit", 0)
            if monthly_limit > 0:
                limit_check = await check_execution_limit(user_id, monthly_limit, redis_client, session)
                if not limit_check["allowed"]:
                    # Hard block — grace period expired
                    now = datetime.now(timezone.utc)
                    await session.execute(
                        update(Execution)
                        .where(Execution.id == execution_id)
                        .values(
                            status="failed",
                            error_message="Monthly execution limit reached",
                            last_attempt_at=now,
                            updated_at=now,
                        )
                    )
                    await session.commit()
                    logger.warning("Execution blocked: monthly limit reached", extra={
                        "event_type": "execution_blocked",
                        "execution_id": execution_id,
                        "cue_id": cue_id,
                        "user_id": user_id,
                    })
                    return

        # Claim execution — only from 'pending' for initial delivery
        claimed = await _claim_execution(session, execution_id, "pending")
        if not claimed:
            logger.debug("Execution claim failed (duplicate)", extra={
                "event_type": "claim_failed",
                "execution_id": execution_id,
                "cue_id": cue_id,
            })
            return

        # Get current attempt count
        exec_result = await session.execute(
            select(Execution.attempts).where(Execution.id == execution_id)
        )
        exec_row = exec_result.fetchone()
        attempt = (exec_row.attempts if exec_row else 0) + 1

        # Deliver
        t0 = time.monotonic()
        success, http_status, response_text = await deliver_webhook(
            callback_url=payload["callback_url"],
            callback_method=payload["callback_method"],
            callback_headers=payload.get("callback_headers", {}),
            payload=payload.get("payload", {}),
            cue_id=cue_id,
            cue_name=payload.get("cue_name", ""),
            execution_id=execution_id,
            scheduled_for=datetime.fromisoformat(payload["scheduled_for"]),
            attempt=attempt,
            webhook_secret=payload.get("webhook_secret", ""),
        )
        latency = time.monotonic() - t0

        # Increment run_count on every attempt (not just success)
        await session.execute(
            update(Cue)
            .where(Cue.id == cue_id)
            .values(run_count=Cue.run_count + 1)
        )

        if success:
            await _handle_success(
                session, execution_id, http_status, response_text, attempt, cue_id,
                user_id=user_id, redis_client=redis_client,
            )
            logger.info("Webhook delivered", extra={
                "event_type": "webhook_success",
                "cue_id": cue_id,
                "execution_id": execution_id,
                "http_status": http_status,
                "attempt": attempt,
                "latency_ms": int(latency * 1000),
            })
        else:
            max_attempts = payload.get("retry_max_attempts", 3)
            await _handle_failure(
                session,
                execution_id,
                http_status,
                response_text or "Unknown error",
                attempt,
                cue_id,
                max_attempts,
                payload.get("retry_backoff_minutes", [1, 5, 15]),
            )
            logger.warning("Webhook failed", extra={
                "event_type": "webhook_failure",
                "cue_id": cue_id,
                "execution_id": execution_id,
                "error": (response_text or "Unknown error")[:200],
                "attempt": attempt,
                "will_retry": attempt < max_attempts,
                "latency_ms": int(latency * 1000),
            })
    finally:
        await session.close()


async def retry_webhook_task(ctx: dict, payload: dict):
    """arq task: retry a webhook delivery."""
    session = await _get_db_session(ctx)
    redis_client = await _get_redis(ctx)
    try:
        execution_id = payload["execution_id"]
        cue_id = payload["cue_id"]
        user_id = payload.get("user_id")

        # Claim execution — only from 'retry_ready' for retry delivery
        claimed = await _claim_execution(session, execution_id, "retry_ready")
        if not claimed:
            logger.debug("Retry claim failed (duplicate)", extra={
                "event_type": "claim_failed",
                "execution_id": execution_id,
                "cue_id": cue_id,
            })
            return

        # Get current attempt count
        exec_result = await session.execute(
            select(Execution.attempts).where(Execution.id == execution_id)
        )
        exec_row = exec_result.fetchone()
        attempt = (exec_row.attempts if exec_row else 0) + 1

        # Deliver
        t0 = time.monotonic()
        success, http_status, response_text = await deliver_webhook(
            callback_url=payload["callback_url"],
            callback_method=payload["callback_method"],
            callback_headers=payload.get("callback_headers", {}),
            payload=payload.get("payload", {}),
            cue_id=cue_id,
            cue_name=payload.get("cue_name", ""),
            execution_id=execution_id,
            scheduled_for=datetime.fromisoformat(payload["scheduled_for"]),
            attempt=attempt,
            webhook_secret=payload.get("webhook_secret", ""),
        )
        latency = time.monotonic() - t0

        # Increment run_count on every attempt (not just success)
        await session.execute(
            update(Cue)
            .where(Cue.id == cue_id)
            .values(run_count=Cue.run_count + 1)
        )

        if success:
            await _handle_success(
                session, execution_id, http_status, response_text, attempt, cue_id,
                user_id=user_id, redis_client=redis_client,
            )
            logger.info("Webhook retry delivered", extra={
                "event_type": "webhook_success",
                "cue_id": cue_id,
                "execution_id": execution_id,
                "http_status": http_status,
                "attempt": attempt,
                "latency_ms": int(latency * 1000),
            })
        else:
            max_attempts = payload.get("retry_max_attempts", 3)
            await _handle_failure(
                session,
                execution_id,
                http_status,
                response_text or "Unknown error",
                attempt,
                cue_id,
                max_attempts,
                payload.get("retry_backoff_minutes", [1, 5, 15]),
            )
            logger.warning("Webhook retry failed", extra={
                "event_type": "webhook_failure",
                "cue_id": cue_id,
                "execution_id": execution_id,
                "error": (response_text or "Unknown error")[:200],
                "attempt": attempt,
                "will_retry": attempt < max_attempts,
                "latency_ms": int(latency * 1000),
            })
    finally:
        await session.close()
# ── Messaging primitive — push delivery (Phase 12.1.5) ─────────────


async def _check_concurrent_cap_or_recycle(
    session: AsyncSession,
    redis_client: Optional[aioredis.Redis],
    *,
    user_id: str,
    task_type: str,
    payload: dict,
) -> Optional[str]:
    """Per-user concurrent delivery cap (spec §5.6).

    Shares the ``concurrent:{user_id}`` Redis counter with cue webhook
    deliveries — same per-user TOTAL cap covers both. INCRs the
    counter; if over ``settings.MAX_CONCURRENT_DELIVERIES_PER_USER``
    decrements back, inserts a fresh outbox row at
    ``scheduled_at = now + 30s`` to recycle through the dispatcher,
    and returns ``None`` to signal "skip this cycle."

    Returns the concurrent-key (str) when under cap and the caller
    should proceed; ``None`` when over cap (caller should return).

    Caller MUST call ``_release_concurrent`` after the delivery work
    completes, even on exception, to keep the counter accurate.

    Differs from cue-side pattern: for cues, the dispatch_outbox row
    isn't yet marked dispatched at this point, so the cue path
    returns and the poller re-dispatches the same row next cycle.
    For messages, the outbox row was already marked dispatched by
    the dispatcher before we got here, so the cue strategy would
    leave the message permanently queued. Hence the recycle-row
    pattern.
    """
    if not user_id or not redis_client:
        return None  # No cap enforcement when context is incomplete; proceed.
    concurrent_key = f"concurrent:{user_id}"
    try:
        current = await redis_client.incr(concurrent_key)
        # 10-minute safety TTL — if a worker dies after INCR without
        # DECR, the counter resets eventually rather than blocking
        # the user forever.
        await redis_client.expire(concurrent_key, 600)
        if current > settings.MAX_CONCURRENT_DELIVERIES_PER_USER:
            await redis_client.decr(concurrent_key)
            # Recycle: insert a fresh outbox row to dispatch in 30s.
            # Reuses the existing scheduled_at filter from the
            # Slice-3b dispatcher — no new poll loop needed.
            now = datetime.now(timezone.utc)
            recycle_at = now + timedelta(seconds=30)
            session.add(
                DispatchOutbox(
                    execution_id=None,
                    cue_id=None,
                    task_type=task_type,
                    payload=payload,
                    scheduled_at=recycle_at,
                )
            )
            await session.commit()
            logger.warning(
                "per-user concurrent delivery cap reached; recycling message dispatch",
                extra={
                    "event_type": "msg_delivery_concurrent_cap_recycled",
                    "user_id": user_id,
                    "concurrent": current,
                    "cap": settings.MAX_CONCURRENT_DELIVERIES_PER_USER,
                    "task_type": task_type,
                    "message_id": payload.get("message_id"),
                    "next_attempt_at": recycle_at.isoformat(),
                },
            )
            return None
        return concurrent_key
    except Exception as e:
        # Redis blip — let the request through; cap is best-effort.
        logger.warning(
            "concurrent-cap check failed; proceeding without enforcement",
            extra={"user_id": user_id, "error": str(e)},
        )
        return concurrent_key


async def _release_concurrent(
    redis_client: Optional[aioredis.Redis],
    concurrent_key: Optional[str],
):
    """Decrement the concurrent-delivery counter. Best-effort —
    swallows Redis errors. Caller invokes in a ``finally`` block.
    """
    if not redis_client or not concurrent_key:
        return
    try:
        await redis_client.decr(concurrent_key)
    except Exception:
        # Counter is best-effort. Don't let a Redis blip break the
        # delivery state machine.
        pass


async def _load_message_context(
    session: AsyncSession,
    *,
    message_id: str,
    to_agent_id: str,
):
    """Look up message + agents + user live (not from outbox payload).

    Returns (msg, to_agent, from_agent, user) or None if any lookup
    fails OR if to_agent.webhook_url is now NULL (mid-flight rotation
    means the worker no-ops cleanly per §5.1 / PM design 2026-04-30).
    """
    msg = await session.get(Message, message_id)
    if msg is None:
        logger.warning(
            "deliver_message_task: message not found",
            extra={"event_type": "msg_deliver_not_found", "message_id": message_id},
        )
        return None

    to_agent = await session.get(Agent, to_agent_id)
    if to_agent is None:
        logger.warning(
            "deliver_message_task: to_agent not found (deleted?); leaving message in queued",
            extra={
                "event_type": "msg_deliver_to_agent_missing",
                "message_id": message_id,
                "to_agent_id": to_agent_id,
            },
        )
        return None

    if to_agent.webhook_url is None:
        # webhook_url cleared between create and delivery — recipient
        # explicitly opted out. No-op; leave message in queued
        # for poll-fetchers (recipient agency wins).
        logger.info(
            "deliver_message_task: webhook_url cleared mid-flight; leaving message in queued for poll",
            extra={
                "event_type": "msg_deliver_url_cleared",
                "message_id": message_id,
                "to_agent_id": to_agent_id,
            },
        )
        return None

    from_agent = await session.get(Agent, msg.from_agent_id)
    if from_agent is None:
        logger.warning(
            "deliver_message_task: from_agent not found",
            extra={
                "event_type": "msg_deliver_from_agent_missing",
                "message_id": message_id,
                "from_agent_id": msg.from_agent_id,
            },
        )
        return None

    user = await session.get(User, msg.user_id)
    if user is None:
        logger.warning(
            "deliver_message_task: user not found",
            extra={
                "event_type": "msg_deliver_user_missing",
                "message_id": message_id,
                "user_id": str(msg.user_id),
            },
        )
        return None

    return msg, to_agent, from_agent, user


async def _claim_message(
    session: AsyncSession,
    *,
    message_id: str,
    expected_state: str,
) -> bool:
    """Conditional UPDATE: ``expected_state → delivering``.

    Sets ``delivering_started_at = now()`` on the claim so the stale-
    recovery poll loop can detect worker-crash-mid-delivery (§5.4
    stale-recovery semantics; Slice 3b).

    Returns ``True`` if the claim succeeded; ``False`` if another
    worker (or the poll-fetcher in the queued case) won the race.
    """
    now = datetime.now(timezone.utc)
    claim = await session.execute(
        update(Message)
        .where(Message.id == message_id, Message.delivery_state == expected_state)
        .values(delivery_state="delivering", delivering_started_at=now)
        .returning(Message.id)
    )
    if claim.first() is None:
        logger.debug(
            "claim failed (raced; already past expected state)",
            extra={
                "event_type": "msg_deliver_claim_failed",
                "message_id": message_id,
                "expected_state": expected_state,
            },
        )
        await session.commit()
        return False
    await session.commit()
    return True


async def _route_attempt_outcome(
    session: AsyncSession,
    *,
    msg: Message,
    to_agent: Agent,
    attempt: int,
    result: DeliveryAttemptResult,
    latency_ms: int,
):
    """After a delivery attempt completes, classify + route:

    * Success → ``delivered`` (terminal). Clear ``delivering_started_at``.
    * Retryable + budget remaining → insert ``retry_message`` outbox
      row with scheduled_at = now + backoff (honoring Retry-After).
      Transition message to ``retry_ready``.
    * Retryable + budget exhausted → ``failed`` (retries exhausted).
    * Terminal → ``failed`` (4xx-terminal or unexpected error).

    All log events use structured ``extra={...}`` fields per PM's
    audit-backfill ask: ``message_id``, ``attempt_number``,
    ``status_code``, ``retry_after_seconds``, ``next_attempt_at``,
    ``error_type``, ``latency_ms``. A future ``delivery_attempts``
    audit table can backfill from log archive.
    """
    classification = result.classification
    now = datetime.now(timezone.utc)
    structured = {
        "event_type": classification.log_event_type,
        "message_id": msg.id,
        "to_agent_id": to_agent.id,
        "attempt_number": attempt,
        "status_code": classification.http_status,
        "error_type": classification.error_type,
        "latency_ms": latency_ms,
    }

    if classification.is_success:
        await session.execute(
            update(Message)
            .where(Message.id == msg.id)
            .values(
                delivery_state="delivered",
                delivered_at=now,
                delivering_started_at=None,
            )
        )
        await session.commit()
        logger.info("message delivered", extra=structured)
        return

    if classification.is_retryable and attempt < MESSAGE_RETRY_MAX_ATTEMPTS + 1:
        # Schedule the next attempt. Backoff index = attempt - 1
        # clamped to len(backoff)-1; e.g. attempt 1 → 1 min; attempt
        # 2 → 5 min; attempt 3 → 15 min; further attempts (none in
        # v1.5) reuse the last value.
        idx = min(attempt - 1, len(MESSAGE_RETRY_BACKOFF_MINUTES) - 1)
        own_min_seconds = MESSAGE_RETRY_BACKOFF_MINUTES[idx] * 60
        # Honor 429 / 503 Retry-After if present:
        # ``max(own_min, retry_after)`` per Max OpenClaw's review.
        delay_seconds = parse_retry_after(
            result.retry_after_header,
            own_min_seconds=own_min_seconds,
        )
        scheduled_at = now + timedelta(seconds=delay_seconds)
        next_attempt = attempt + 1

        # Insert retry_message outbox row deferred to scheduled_at.
        # Worker re-reads to_agent.webhook_url + secret live on each
        # attempt (§5.1), so storing webhook_url here is for
        # discoverability / debugging only — not load-bearing.
        session.add(
            DispatchOutbox(
                execution_id=None,
                cue_id=None,
                task_type="retry_message",
                payload={
                    "message_id": msg.id,
                    "to_agent_id": to_agent.id,
                    "webhook_url": to_agent.webhook_url,
                    "attempt": next_attempt,
                },
                scheduled_at=scheduled_at,
            )
        )
        await session.execute(
            update(Message)
            .where(Message.id == msg.id)
            .values(delivery_state="retry_ready", delivering_started_at=None)
        )
        await session.commit()

        retry_event = (
            EVT_429_RETRY_AFTER
            if result.retry_after_header is not None
            else EVT_RETRY_SCHEDULED
        )
        logger.info(
            "message retry scheduled",
            extra={
                **structured,
                "event_type": retry_event,
                "retry_after_seconds": delay_seconds,
                "retry_after_header": result.retry_after_header,
                "next_attempt_number": next_attempt,
                "next_attempt_at": scheduled_at.isoformat(),
            },
        )
        return

    # Terminal: either non-retryable (4xx-terminal) OR retries exhausted.
    failure_event = (
        EVT_RETRIES_EXHAUSTED
        if classification.is_retryable
        else classification.log_event_type
    )
    await session.execute(
        update(Message)
        .where(Message.id == msg.id)
        .values(
            delivery_state="failed",
            failed_at=now,
            delivering_started_at=None,
        )
    )
    await session.commit()
    logger.warning(
        "message delivery failed (terminal)",
        extra={
            **structured,
            "event_type": failure_event,
            "error_message": classification.error_message,
            "response_excerpt": (result.response_body or "")[:200],
        },
    )


async def deliver_message_task(ctx: dict, payload: dict):
    """arq task: push-deliver a message to ``to_agent.webhook_url``.

    Spec: <https://trydock.ai/mike/cueapi-messaging-primitive-v1-sp>
    §5 (Push delivery).

    Behavior (Slice 3b — current):

    * Look up message + agents + user LIVE (not from outbox payload)
      per §5.1 (secret rotation safety).
    * If ``to_agent.webhook_url`` was cleared between create and
      delivery → no-op, leave message in ``queued`` for poll-fetchers.
    * Otherwise claim ``queued → delivering`` atomically and POST.
    * Classify outcome via ``classify_response`` / ``classify_exception``
      (granular taxonomy from §5.4 — 401/404/405 distinct terminal,
      502/503 retryable, TLS handshake / DNS / connection refused
      distinct).
    * On success → ``delivered``.
    * On retryable failure with budget remaining → insert
      ``retry_message`` outbox row at ``scheduled_at = now + backoff``
      (honoring 429 / 503 ``Retry-After`` per Max's review:
      ``max(own_min, retry_after)``). Transition message to
      ``retry_ready``.
    * On retryable failure with budget exhausted → ``failed``.
    * On terminal failure (4xx-terminal) → ``failed`` immediately.

    Stale-recovery for worker-crash-mid-delivery is handled by a
    separate poll loop in ``worker/poller.py`` that scans messages
    stuck in ``delivering`` past
    ``MESSAGE_DELIVERY_STALE_AFTER_SECONDS`` and moves them back to
    ``retry_ready``.
    """
    message_id = payload["message_id"]
    to_agent_id = payload["to_agent_id"]

    session = await _get_db_session(ctx)
    redis_client = await _get_redis(ctx)
    try:
        ctx_load = await _load_message_context(
            session, message_id=message_id, to_agent_id=to_agent_id
        )
        if ctx_load is None:
            return
        msg, to_agent, from_agent, user = ctx_load

        # Per-user concurrent delivery cap (spec §5.6). Check BEFORE
        # claim so we don't change message state if over cap. The
        # helper inserts a recycle outbox row + returns None when
        # capped; otherwise returns the concurrent_key for release.
        concurrent_key = await _check_concurrent_cap_or_recycle(
            session,
            redis_client,
            user_id=str(user.id),
            task_type="deliver_message",
            payload=payload,
        )
        if concurrent_key is None and user.id and redis_client:
            # Capped + recycled. Stop here.
            return

        try:
            # Claim: queued → delivering atomically, set delivering_started_at.
            if not await _claim_message(session, message_id=message_id, expected_state="queued"):
                return
            # Re-fetch the message after claim so callers see the new state
            # in the in-memory object (delivering_started_at populated).
            await session.refresh(msg)

            attempt = 1
            t0 = time.monotonic()
            result = await deliver_message_to_webhook(
                msg=msg,
                from_agent=from_agent,
                to_agent=to_agent,
                # v1 is same-tenant only — sender and recipient share the
                # same user, so both slug-form addresses anchor on
                # ``user.slug``.
                sender_user_slug=user.slug,
                recipient_user_slug=user.slug,
                attempt=attempt,
            )
            latency_ms = int((time.monotonic() - t0) * 1000)

            await _route_attempt_outcome(
                session,
                msg=msg,
                to_agent=to_agent,
                attempt=attempt,
                result=result,
                latency_ms=latency_ms,
            )
        finally:
            await _release_concurrent(redis_client, concurrent_key)
    finally:
        await session.close()


async def retry_message_task(ctx: dict, payload: dict):
    """arq task: retry a message delivery after a previous attempt
    failed with a retryable error.

    Same shape as ``deliver_message_task`` but:

    * Claims from ``retry_ready → delivering`` (instead of queued).
    * Reads ``attempt`` from ``payload`` (set by the prior attempt
      when it inserted this retry row); attempt count carries through
      so logs / X-CueAPI-Attempt header reflect the actual try number.

    Recurses by inserting another ``retry_message`` outbox row if
    the budget is not exhausted; terminates with ``failed`` otherwise.

    Spec: <https://trydock.ai/mike/cueapi-messaging-primitive-v1-sp>
    §5.4.
    """
    message_id = payload["message_id"]
    to_agent_id = payload["to_agent_id"]
    attempt = payload.get("attempt", 2)  # default sane fallback

    session = await _get_db_session(ctx)
    redis_client = await _get_redis(ctx)
    try:
        ctx_load = await _load_message_context(
            session, message_id=message_id, to_agent_id=to_agent_id
        )
        if ctx_load is None:
            return
        msg, to_agent, from_agent, user = ctx_load

        # Per-user concurrent delivery cap (spec §5.6). Same as
        # deliver_message_task — recycle outbox row + return if capped.
        concurrent_key = await _check_concurrent_cap_or_recycle(
            session,
            redis_client,
            user_id=str(user.id),
            task_type="retry_message",
            payload=payload,
        )
        if concurrent_key is None and user.id and redis_client:
            return

        try:
            # Claim: retry_ready → delivering atomically.
            if not await _claim_message(
                session, message_id=message_id, expected_state="retry_ready"
            ):
                return
            await session.refresh(msg)

            t0 = time.monotonic()
            result = await deliver_message_to_webhook(
                msg=msg,
                from_agent=from_agent,
                to_agent=to_agent,
                sender_user_slug=user.slug,
                recipient_user_slug=user.slug,
                attempt=attempt,
            )
            latency_ms = int((time.monotonic() - t0) * 1000)

            await _route_attempt_outcome(
                session,
                msg=msg,
                to_agent=to_agent,
                attempt=attempt,
                result=result,
                latency_ms=latency_ms,
            )
        finally:
            await _release_concurrent(redis_client, concurrent_key)
    finally:
        await session.close()
