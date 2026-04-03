from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytz
import redis.asyncio as aioredis
from arq import create_pool
from arq.connections import RedisSettings
from croniter import croniter
from sqlalchemy import delete, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models.cue import Cue
from app.models.device_code import DeviceCode
from app.models.dispatch_outbox import DispatchOutbox
from app.models.execution import Execution
from app.models.user import User
from app.models.worker import Worker

from worker.tasks import _send_failure_email, _send_failure_webhook

logger = logging.getLogger(__name__)

MAX_DRAIN_ITERATIONS = 20


async def _run_on_failure_escalation(db_engine, cue_id: str, execution_id: str, error_message: str):
    """Run on_failure escalation (email, webhook, pause) for a failed execution.

    Called from stale recovery and unclaimed-worker-failure paths which
    bypass the normal _handle_failure() in tasks.py.
    """
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with session_factory() as session:
        try:
            # Fetch cue details for escalation
            cue_result = await session.execute(
                select(
                    Cue.name, Cue.user_id, Cue.on_failure, Cue.schedule_type,
                ).where(Cue.id == cue_id)
            )
            cue_row = cue_result.fetchone()
            if not cue_row:
                return

            on_failure = cue_row.on_failure if cue_row.on_failure else {}
            on_failure_email = on_failure.get("email", True)
            on_failure_webhook = on_failure.get("webhook")
            on_failure_pause = on_failure.get("pause", False)

            # on_failure.pause: pause the cue after final failure
            if on_failure_pause and cue_row.schedule_type != "once":
                now = datetime.now(timezone.utc)
                await session.execute(
                    update(Cue)
                    .where(Cue.id == cue_id)
                    .values(status="paused", next_run=None, updated_at=now)
                )
                await session.commit()

            # on_failure.email: send failure notification
            if on_failure_email:
                await _send_failure_email(
                    session, str(cue_row.user_id), cue_id,
                    cue_row.name or cue_id, execution_id, error_message,
                )

            # on_failure.webhook: POST failure details
            if on_failure_webhook:
                await _send_failure_webhook(
                    on_failure_webhook, cue_id,
                    cue_row.name or cue_id, 0,
                    None, error_message, datetime.now(timezone.utc),
                )
        except Exception as e:
            logger.error(f"on_failure escalation failed for execution {execution_id}: {e}")


def _get_next_run_for_cron(expression: str, timezone_str: str = "UTC", after: Optional[datetime] = None) -> datetime:
    tz = pytz.timezone(timezone_str)
    base = after or datetime.now(tz)
    if base.tzinfo is None:
        base = tz.localize(base)
    else:
        base = base.astimezone(tz)
    cron = croniter(expression, base)
    return cron.get_next(datetime).astimezone(pytz.utc)


async def poll_due_cues(db_engine, batch_size: int = 500) -> int:
    """Drain loop: keeps querying until no due cues remain."""
    total = 0
    for _ in range(MAX_DRAIN_ITERATIONS):
        count = await _process_cue_batch(db_engine, batch_size)
        total += count
        if count < batch_size:
            break
    return total


async def _process_cue_batch(db_engine, batch_size: int) -> int:
    now = datetime.now(timezone.utc)
    count = 0

    async with db_engine.begin() as conn:
        # SELECT due cues FOR UPDATE SKIP LOCKED
        result = await conn.execute(
            select(
                Cue.id,
                Cue.name,
                Cue.user_id,
                Cue.schedule_type,
                Cue.schedule_cron,
                Cue.schedule_timezone,
                Cue.next_run,
                Cue.callback_url,
                Cue.callback_method,
                Cue.callback_headers,
                Cue.callback_transport,
                Cue.payload,
                Cue.retry_max_attempts,
                Cue.retry_backoff_minutes,
            )
            .where(
                Cue.status == "active",
                Cue.next_run <= now,
                Cue.next_run.isnot(None),
            )
            .order_by(Cue.next_run)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        due_cues = result.fetchall()

        for cue in due_cues:
            execution_id = uuid.uuid4()
            scheduled_for = cue.next_run

            # INSERT execution with ON CONFLICT DO NOTHING (dedup)
            exec_result = await conn.execute(
                pg_insert(Execution)
                .values(
                    id=execution_id,
                    cue_id=cue.id,
                    scheduled_for=scheduled_for,
                    status="pending",
                )
                .on_conflict_do_nothing(index_elements=["cue_id", "scheduled_for"])
                .returning(Execution.id)
            )
            inserted = exec_result.fetchone()

            if inserted:
                # Increment fired_count for this cue
                await conn.execute(
                    update(Cue)
                    .where(Cue.id == cue.id)
                    .values(fired_count=Cue.fired_count + 1)
                )

                # Skip outbox dispatch for worker-transport cues — they sit in
                # 'pending' and are pulled by the worker daemon via GET /claimable.
                if cue.callback_transport == "worker":
                    pass  # No outbox row, no arq dispatch
                else:
                    # Look up user's monthly execution limit and webhook secret
                    user_result = await conn.execute(
                        select(
                            User.monthly_execution_limit,
                            User.webhook_secret,
                        ).where(User.id == cue.user_id)
                    )
                    user_row = user_result.fetchone()
                    monthly_limit = user_row.monthly_execution_limit if user_row else 0
                    webhook_secret = user_row.webhook_secret if user_row else ""

                    # INSERT outbox row in SAME transaction
                    await conn.execute(
                        pg_insert(DispatchOutbox).values(
                            execution_id=inserted[0],
                            cue_id=cue.id,
                            task_type="deliver",
                            payload={
                                "cue_id": cue.id,
                                "cue_name": cue.name,
                                "user_id": str(cue.user_id),
                                "execution_id": str(inserted[0]),
                                "scheduled_for": scheduled_for.isoformat(),
                                "callback_url": cue.callback_url,
                                "callback_method": cue.callback_method,
                                "callback_headers": cue.callback_headers or {},
                                "payload": cue.payload or {},
                                "retry_max_attempts": cue.retry_max_attempts,
                                "retry_backoff_minutes": cue.retry_backoff_minutes,
                                "monthly_execution_limit": monthly_limit,
                                "webhook_secret": webhook_secret,
                            },
                        )
                    )

            # Update cue next_run
            if cue.schedule_type == "recurring" and cue.schedule_cron:
                new_next_run = _get_next_run_for_cron(
                    cue.schedule_cron, cue.schedule_timezone, after=scheduled_for
                )
                await conn.execute(
                    update(Cue)
                    .where(Cue.id == cue.id)
                    .values(next_run=new_next_run, updated_at=now)
                )
            else:
                # One-time cue: set next_run to NULL (poller won't pick it up again)
                await conn.execute(
                    update(Cue)
                    .where(Cue.id == cue.id)
                    .values(next_run=None, updated_at=now)
                )

            count += 1

    return count


async def poll_retries(db_engine, batch_size: int = 500) -> int:
    """Pick up executions that are due for retry."""
    now = datetime.now(timezone.utc)
    count = 0

    async with db_engine.begin() as conn:
        result = await conn.execute(
            select(Execution.id, Execution.cue_id)
            .where(
                Execution.status == "retrying",
                Execution.next_retry <= now,
                Execution.next_retry.isnot(None),
            )
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        retries = result.fetchall()

        for execution in retries:
            # Get cue info for the outbox payload
            cue_result = await conn.execute(
                select(
                    Cue.id,
                    Cue.name,
                    Cue.callback_url,
                    Cue.callback_method,
                    Cue.callback_headers,
                    Cue.payload,
                    Cue.retry_max_attempts,
                    Cue.retry_backoff_minutes,
                ).where(Cue.id == execution.cue_id)
            )
            cue = cue_result.fetchone()
            if not cue:
                continue

            # Get execution details
            exec_result = await conn.execute(
                select(Execution.scheduled_for, Execution.attempts)
                .where(Execution.id == execution.id)
            )
            exec_row = exec_result.fetchone()

            # Look up user_id and webhook_secret from cue's user
            cue_user_result = await conn.execute(
                select(Cue.user_id).where(Cue.id == execution.cue_id)
            )
            cue_user_row = cue_user_result.fetchone()
            cue_user_id = str(cue_user_row.user_id) if cue_user_row else None

            # Fetch user's webhook_secret for signing
            webhook_secret = ""
            if cue_user_id:
                ws_result = await conn.execute(
                    select(User.webhook_secret).where(User.id == cue_user_row.user_id)
                )
                ws_row = ws_result.fetchone()
                webhook_secret = ws_row.webhook_secret if ws_row else ""

            # INSERT outbox row
            await conn.execute(
                pg_insert(DispatchOutbox).values(
                    execution_id=execution.id,
                    cue_id=execution.cue_id,
                    task_type="retry",
                    payload={
                        "cue_id": cue.id,
                        "cue_name": cue.name,
                        "user_id": cue_user_id,
                        "execution_id": str(execution.id),
                        "scheduled_for": exec_row.scheduled_for.isoformat(),
                        "callback_url": cue.callback_url,
                        "callback_method": cue.callback_method,
                        "callback_headers": cue.callback_headers or {},
                        "payload": cue.payload or {},
                        "retry_max_attempts": cue.retry_max_attempts,
                        "retry_backoff_minutes": cue.retry_backoff_minutes,
                        "webhook_secret": webhook_secret,
                    },
                )
            )

            # Set execution status to 'retry_ready' to prevent re-pickup by retry poller.
            # The worker will claim from 'retry_ready' → 'delivering'.
            await conn.execute(
                update(Execution)
                .where(Execution.id == execution.id)
                .values(status="retry_ready", updated_at=now)
            )

            count += 1

    return count


async def dispatch_outbox(db_engine, arq_redis, batch_size: int = 500) -> int:
    """Move undispatched outbox rows to the arq Redis queue."""
    now = datetime.now(timezone.utc)
    count = 0

    async with db_engine.begin() as conn:
        result = await conn.execute(
            select(
                DispatchOutbox.id,
                DispatchOutbox.execution_id,
                DispatchOutbox.cue_id,
                DispatchOutbox.task_type,
                DispatchOutbox.payload,
            )
            .where(DispatchOutbox.dispatched == False)  # noqa: E712
            .order_by(DispatchOutbox.created_at)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        rows = result.fetchall()

        for row in rows:
            try:
                task_name = "deliver_webhook_task" if row.task_type == "deliver" else "retry_webhook_task"
                await arq_redis.enqueue_job(
                    task_name,
                    row.payload,
                )
                # Mark dispatched
                await conn.execute(
                    update(DispatchOutbox)
                    .where(DispatchOutbox.id == row.id)
                    .values(dispatched=True)
                )
                count += 1
            except Exception as e:
                logger.error(f"Failed to dispatch outbox row {row.id}: {e}")
                await conn.execute(
                    update(DispatchOutbox)
                    .where(DispatchOutbox.id == row.id)
                    .values(
                        dispatch_attempts=DispatchOutbox.dispatch_attempts + 1,
                        last_dispatch_error=str(e)[:500],
                    )
                )

    return count


async def recover_stale_executions(db_engine, stale_seconds: int = 300) -> int:
    """Find executions stuck in 'delivering' beyond threshold. Recover or fail them."""
    stale_cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)
    count = 0
    # Collect final failures for on_failure escalation after transaction commits
    escalations = []

    async with db_engine.begin() as conn:
        result = await conn.execute(
            select(
                Execution.id,
                Execution.cue_id,
                Execution.attempts,
            )
            .where(
                Execution.status == "delivering",
                Execution.updated_at < stale_cutoff,
            )
            .limit(100)
            .with_for_update(skip_locked=True)
        )
        stale = result.fetchall()

        now = datetime.now(timezone.utc)
        for execution in stale:
            # Look up cue for retry config
            cue_result = await conn.execute(
                select(
                    Cue.retry_max_attempts,
                    Cue.retry_backoff_minutes,
                    Cue.schedule_type,
                ).where(Cue.id == execution.cue_id)
            )
            cue = cue_result.fetchone()
            max_attempts = cue.retry_max_attempts if cue else 3
            backoff_minutes = cue.retry_backoff_minutes if cue else [1, 5, 15]

            if execution.attempts < max_attempts:
                # Has attempts remaining — move to retrying for retry poller to pick up
                idx = min(execution.attempts, len(backoff_minutes) - 1)
                next_retry = now + timedelta(minutes=backoff_minutes[idx])
                await conn.execute(
                    update(Execution)
                    .where(Execution.id == execution.id)
                    .values(
                        status="retrying",
                        next_retry=next_retry,
                        error_message=f"Recovered from stale delivering state after {stale_seconds}s",
                        updated_at=now,
                    )
                )
                logger.warning(f"Recovered stale execution {execution.id} → retrying")
            else:
                # Max attempts exhausted — fail
                error_msg = f"Failed: stuck in delivering for >{stale_seconds}s, max attempts exhausted"
                await conn.execute(
                    update(Execution)
                    .where(Execution.id == execution.id)
                    .values(
                        status="failed",
                        error_message=error_msg,
                        next_retry=None,
                        updated_at=now,
                    )
                )
                # If one-time cue → mark failed
                if cue and cue.schedule_type == "once":
                    await conn.execute(
                        update(Cue)
                        .where(Cue.id == execution.cue_id)
                        .values(status="failed", updated_at=now)
                    )
                logger.error(f"Failed stale execution {execution.id} — max attempts exhausted")
                escalations.append((execution.cue_id, str(execution.id), error_msg))

            count += 1

    # Run on_failure escalation after transaction is committed
    for cue_id, exec_id, error_msg in escalations:
        await _run_on_failure_escalation(db_engine, cue_id, exec_id, error_msg)

    return count


async def recover_stale_worker_claims(
    db_engine,
    heartbeat_timeout: int = 180,
    claim_timeout: int = 900,
) -> int:
    """Reset worker-claimed executions when the worker is stale AND the claim lease has expired.

    BOTH conditions must be true:
    - Worker's last_heartbeat is older than heartbeat_timeout seconds
    - Execution's claimed_at is older than claim_timeout seconds

    This prevents reclaiming from live workers running long handlers.
    """
    now = datetime.now(timezone.utc)
    heartbeat_cutoff = now - timedelta(seconds=heartbeat_timeout)
    claim_cutoff = now - timedelta(seconds=claim_timeout)
    count = 0

    async with db_engine.begin() as conn:
        # Find stale worker-claimed executions
        result = await conn.execute(
            select(
                Execution.id,
                Execution.cue_id,
                Execution.claimed_by_worker,
            )
            .join(Cue, Execution.cue_id == Cue.id)
            .where(
                Execution.status == "delivering",
                Cue.callback_transport == "worker",
                Execution.claimed_at < claim_cutoff,
                Execution.claimed_by_worker.isnot(None),
            )
            .limit(100)
        )
        stale = result.fetchall()

        for execution in stale:
            # Check if the worker's heartbeat is stale
            worker_result = await conn.execute(
                select(Worker.last_heartbeat)
                .join(Cue, Cue.user_id == Worker.user_id)
                .where(
                    Cue.id == execution.cue_id,
                    Worker.worker_id == execution.claimed_by_worker,
                )
            )
            worker_row = worker_result.fetchone()

            # If worker doesn't exist or heartbeat is stale, reset the execution
            if worker_row is None or worker_row.last_heartbeat < heartbeat_cutoff:
                await conn.execute(
                    update(Execution)
                    .where(Execution.id == execution.id)
                    .values(
                        status="pending",
                        claimed_by_worker=None,
                        claimed_at=None,
                        started_at=None,
                        error_message="Reset: worker heartbeat stale and claim lease expired",
                        updated_at=now,
                    )
                )
                logger.warning(
                    f"Reset stale worker claim on execution {execution.id} "
                    f"(worker: {execution.claimed_by_worker})"
                )
                count += 1

    return count


async def fail_unclaimed_worker_executions(
    db_engine,
    unclaimed_timeout: int = 900,
) -> int:
    """Fail pending worker-transport executions that have been unclaimed too long."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=unclaimed_timeout)
    count = 0
    escalations = []
    error_msg = "No worker claimed this execution within timeout window"

    async with db_engine.begin() as conn:
        result = await conn.execute(
            select(
                Execution.id,
                Execution.cue_id,
            )
            .join(Cue, Execution.cue_id == Cue.id)
            .where(
                Execution.status == "pending",
                Cue.callback_transport == "worker",
                Execution.created_at < cutoff,
                Execution.claimed_by_worker.is_(None),
            )
            .limit(100)
        )
        unclaimed = result.fetchall()

        for execution in unclaimed:
            await conn.execute(
                update(Execution)
                .where(Execution.id == execution.id)
                .values(
                    status="missed",
                    error_message=error_msg,
                    updated_at=now,
                )
            )

            # If one-time cue, mark cue as failed too
            cue_result = await conn.execute(
                select(Cue.schedule_type).where(Cue.id == execution.cue_id)
            )
            cue_row = cue_result.fetchone()
            if cue_row and cue_row.schedule_type == "once":
                await conn.execute(
                    update(Cue)
                    .where(Cue.id == execution.cue_id)
                    .values(status="failed", updated_at=now)
                )

            logger.warning(f"Missed unclaimed worker execution {execution.id}")
            escalations.append((execution.cue_id, str(execution.id)))
            count += 1

    # Run on_failure escalation after transaction is committed
    for cue_id, exec_id in escalations:
        await _run_on_failure_escalation(db_engine, cue_id, exec_id, error_msg)

    return count


async def check_worker_health(db_engine, redis, offline_threshold: int = 300) -> int:
    """Check for workers that went offline and send email alerts.

    Sends an alert when a worker's last heartbeat exceeds offline_threshold
    seconds. Uses a Redis key to prevent duplicate alerts (1h TTL).

    Returns the number of alerts sent.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=offline_threshold)
    alerts_sent = 0

    async with db_engine.begin() as conn:
        # Find workers with stale heartbeats
        result = await conn.execute(
            select(
                Worker.user_id,
                Worker.worker_id,
                Worker.last_heartbeat,
            ).where(
                Worker.last_heartbeat < cutoff,
            )
        )
        stale_workers = result.fetchall()

        for worker_row in stale_workers:
            user_id = str(worker_row.user_id)
            worker_id = worker_row.worker_id
            alert_key = f"worker_alert:{user_id}:{worker_id}"

            # Check if we already sent an alert (1h cooldown)
            already_alerted = await redis.get(alert_key)
            if already_alerted:
                continue

            # Count pending worker executions for this user
            pending_result = await conn.execute(
                select(Execution.id)
                .join(Cue, Execution.cue_id == Cue.id)
                .where(
                    Cue.user_id == worker_row.user_id,
                    Cue.callback_transport == "worker",
                    Execution.status == "pending",
                )
            )
            pending_count = len(pending_result.fetchall())

            # Get user email
            user_result = await conn.execute(
                select(User.email).where(User.id == worker_row.user_id)
            )
            user_row = user_result.fetchone()
            if not user_row or not user_row.email:
                continue

            minutes_offline = int((now - worker_row.last_heartbeat).total_seconds() / 60)

            # Rate limit: max 1 worker offline email per worker per hour
            rate_key = f"worker_offline_email:{worker_id}"
            already_sent = await redis.get(rate_key)
            if already_sent:
                logger.info("Worker offline email suppressed (rate limited): %s", worker_id)
                continue
            await redis.setex(rate_key, 3600, "1")

            # Send alert email
            try:
                import resend
                from app.utils.templates import brand_email, worker_down_email_body

                if settings.RESEND_API_KEY:
                    resend.api_key = settings.RESEND_API_KEY
                    body_html = worker_down_email_body(worker_id, minutes_offline, pending_count)
                    resend.Emails.send({
                        "from": settings.RESEND_FROM_EMAIL,
                        "to": [user_row.email],
                        "subject": f"CueAPI: Worker {worker_id} offline",
                        "html": brand_email("Worker Offline", body_html),
                    })
                    logger.info(
                        "Worker-down alert sent",
                        extra={
                            "event_type": "worker_down_alert",
                            "worker_id": worker_id,
                            "email": user_row.email,
                            "minutes_offline": minutes_offline,
                            "pending_count": pending_count,
                        },
                    )
                    alerts_sent += 1
                else:
                    logger.warning(
                        "Worker %s offline %d min (%d pending) — no RESEND_API_KEY, skipping email",
                        worker_id, minutes_offline, pending_count,
                    )
            except Exception as e:
                logger.error("Failed to send worker-down alert for %s: %s", worker_id, e)

            # Set cooldown regardless of send success to avoid spam
            await redis.set(alert_key, "1", ex=3600)

    return alerts_sent


async def cleanup_outbox(db_engine, retention_days=7):
    """Delete dispatched outbox rows older than retention period."""
    async with db_engine.begin() as conn:
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        result = await conn.execute(
            delete(DispatchOutbox)
            .where(
                DispatchOutbox.dispatched == True,  # noqa: E712
                DispatchOutbox.created_at < cutoff,
            )
        )
        deleted = result.rowcount
        if deleted > 0:
            logger.info("Outbox cleanup complete", extra={
                "event_type": "outbox_cleanup",
                "deleted_count": deleted,
            })
        return deleted


async def cleanup_device_codes(db_engine):
    """Delete expired device codes older than 24 hours."""
    async with db_engine.begin() as conn:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        result = await conn.execute(
            delete(DeviceCode)
            .where(DeviceCode.expires_at < cutoff)
        )
        deleted = result.rowcount
        if deleted > 0:
            logger.info("Device code cleanup complete", extra={
                "event_type": "device_code_cleanup",
                "deleted_count": deleted,
            })
        return deleted


_last_cleanup = datetime.min.replace(tzinfo=timezone.utc)

_REPLICA_ID = os.getenv("RAILWAY_REPLICA_ID", "default")


async def acquire_poller_lock(redis: aioredis.Redis) -> bool:
    """Attempt to acquire poller leadership. Returns True if this is the leader."""
    acquired = await redis.set(
        "poller:leader",
        _REPLICA_ID,
        nx=True,
        ex=settings.POLLER_LEADER_LOCK_TTL_SECONDS,
    )
    return acquired is not None


async def renew_poller_lock(redis: aioredis.Redis) -> bool:
    """Renew leadership. Returns False if lock was stolen."""
    current = await redis.get("poller:leader")
    if current and current == _REPLICA_ID:
        await redis.expire("poller:leader", settings.POLLER_LEADER_LOCK_TTL_SECONDS)
        return True
    return False


async def write_poller_heartbeat(
    redis: aioredis.Redis, cue_count: int, cycle_duration_ms: int
) -> None:
    """Write poller heartbeat metrics to Redis."""
    ttl = settings.POLLER_HEARTBEAT_TTL_SECONDS
    pipe = redis.pipeline()
    pipe.set("poller:last_run", datetime.now(timezone.utc).isoformat(), ex=ttl)
    pipe.set("poller:cues_processed", str(cue_count), ex=ttl)
    pipe.set("poller:cycle_duration_ms", str(cycle_duration_ms), ex=ttl)
    await pipe.execute()


async def run_poller():
    """Main poller loop — runs all four polling functions + hourly cleanup."""
    global _last_cleanup

    from app.utils.logging import setup_logging
    setup_logging()

    logger.info("Starting CueAPI poller...", extra={
        "event_type": "poller_start",
        "replica_id": _REPLICA_ID,
    })

    db_engine = create_async_engine(
        settings.async_database_url,
        pool_size=settings.DATABASE_POOL_SIZE,
        max_overflow=settings.DATABASE_MAX_OVERFLOW,
    )

    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
    arq_redis = await create_pool(redis_settings)

    # Standalone Redis client for heartbeat + leader election (arq pool is separate)
    heartbeat_redis = aioredis.from_url(settings.REDIS_URL, decode_responses=True)

    try:
        while True:
            try:
                # Leader election: only one poller instance runs at a time
                is_leader = await acquire_poller_lock(heartbeat_redis)
                if not is_leader:
                    can_renew = await renew_poller_lock(heartbeat_redis)
                    if not can_renew:
                        logger.warning("Not the poller leader. Sleeping.", extra={
                            "event_type": "poller_standby",
                            "replica_id": _REPLICA_ID,
                        })
                        await asyncio.sleep(settings.POLLER_INTERVAL_SECONDS)
                        continue

                cycle_start = time.monotonic()

                cue_count = await poll_due_cues(db_engine, settings.POLLER_BATCH_SIZE)
                retry_count = await poll_retries(db_engine, settings.POLLER_BATCH_SIZE)
                stale_count = await recover_stale_executions(db_engine, settings.EXECUTION_STALE_AFTER_SECONDS)
                worker_stale_count = await recover_stale_worker_claims(
                    db_engine,
                    heartbeat_timeout=settings.WORKER_HEARTBEAT_TIMEOUT_SECONDS,
                    claim_timeout=settings.WORKER_CLAIM_TIMEOUT_SECONDS,
                )
                worker_unclaimed_count = await fail_unclaimed_worker_executions(
                    db_engine,
                    unclaimed_timeout=settings.WORKER_UNCLAIMED_TIMEOUT_SECONDS,
                )
                dispatch_count = await dispatch_outbox(db_engine, arq_redis, settings.POLLER_BATCH_SIZE)
                worker_alerts = await check_worker_health(db_engine, heartbeat_redis)

                cycle_duration_ms = int((time.monotonic() - cycle_start) * 1000)
                total_processed = cue_count + retry_count

                # Write heartbeat to Redis
                await write_poller_heartbeat(heartbeat_redis, total_processed, cycle_duration_ms)

                # Renew leader lock after work
                await renew_poller_lock(heartbeat_redis)

                # Log cycle metrics (even if all zeros, for heartbeat)
                logger.info("Poller cycle", extra={
                    "event_type": "poller_cycle",
                    "cues_processed": cue_count,
                    "retries_processed": retry_count,
                    "stale_recovered": stale_count,
                    "worker_stale_recovered": worker_stale_count,
                    "worker_unclaimed_failed": worker_unclaimed_count,
                    "dispatched": dispatch_count,
                    "worker_alerts": worker_alerts,
                    "cycle_duration_ms": cycle_duration_ms,
                })

                # Hourly cleanup
                now = datetime.now(timezone.utc)
                if (now - _last_cleanup).total_seconds() > 3600:
                    await cleanup_outbox(db_engine)
                    await cleanup_device_codes(db_engine)
                    _last_cleanup = now

            except Exception:
                logger.exception("Error in poller cycle")

            await asyncio.sleep(settings.POLLER_INTERVAL_SECONDS)
    finally:
        await heartbeat_redis.aclose()
        await arq_redis.close()
        await db_engine.dispose()


if __name__ == "__main__":
    from app.utils.logging import setup_logging
    setup_logging()
    asyncio.run(run_poller())
