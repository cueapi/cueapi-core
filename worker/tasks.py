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
from app.models.cue import Cue
from app.models.execution import Execution
from app.models.user import User
from app.services.usage_service import check_execution_limit, increment_usage
from app.services.webhook import deliver_webhook

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
