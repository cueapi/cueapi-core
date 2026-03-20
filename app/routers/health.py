from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text

from app.database import async_session
from app.models.cue import Cue
from app.models.execution import Execution
from app.models.worker import Worker
from app.redis import get_redis

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check():
    now = datetime.now(timezone.utc)
    overall_status = "healthy"

    # --- Service checks ---
    services = {}

    # Check PostgreSQL — query a real table to catch schema/permission issues
    db_ok = True
    try:
        async with async_session() as session:
            await session.execute(text("SELECT count(*) FROM users LIMIT 1"))
        services["postgres"] = "ok"
    except Exception:
        services["postgres"] = "error"
        overall_status = "degraded"
        db_ok = False

    # Check Redis
    redis = None
    redis_ok = True
    try:
        redis = await get_redis()
        await redis.ping()
        services["redis"] = "ok"
    except Exception:
        services["redis"] = "error"
        overall_status = "degraded"
        redis_ok = False

    # --- Poller status (via Redis heartbeat) ---
    poller_info = {}
    if redis_ok and redis is not None:
        try:
            pipe = redis.pipeline()
            pipe.get("poller:last_run")
            pipe.get("poller:cues_processed")
            pipe.get("poller:cycle_duration_ms")
            results = await pipe.execute()

            poller_last_run_str = results[0]
            poller_cues = results[1]
            poller_duration = results[2]

            if poller_last_run_str is None:
                services["poller"] = "unknown"
                poller_info["status"] = "unknown"
            else:
                poller_last_run = datetime.fromisoformat(poller_last_run_str)
                seconds_ago = (now - poller_last_run).total_seconds()

                poller_info["last_run"] = poller_last_run_str
                poller_info["seconds_ago"] = int(seconds_ago)
                if poller_cues is not None:
                    poller_info["cues_last_cycle"] = int(poller_cues)
                if poller_duration is not None:
                    poller_info["cycle_duration_ms"] = int(poller_duration)

                if seconds_ago > 30:
                    services["poller"] = "stale"
                    overall_status = "degraded"
                else:
                    services["poller"] = "ok"
        except Exception:
            services["poller"] = "unknown"
    else:
        services["poller"] = "unknown"

    # --- Worker status (from workers table) ---
    workers_info = {}
    if db_ok:
        try:
            async with async_session() as session:
                heartbeat_cutoff = now - timedelta(seconds=180)
                result = await session.execute(
                    select(func.count())
                    .select_from(Worker)
                    .where(Worker.last_heartbeat > heartbeat_cutoff)
                )
                active_count = result.scalar() or 0
                workers_info["active_count"] = active_count

                if active_count > 0:
                    services["worker"] = "ok"
                    # Get latest heartbeat
                    latest = await session.execute(
                        select(func.max(Worker.last_heartbeat))
                    )
                    last_hb = latest.scalar()
                    if last_hb:
                        workers_info["last_heartbeat"] = last_hb.isoformat() + "Z"
                else:
                    services["worker"] = "none"
                    # Not degraded — webhook-only users don't need workers
        except Exception:
            services["worker"] = "unknown"
    else:
        services["worker"] = "unknown"

    # --- Queue metrics (best effort, only if DB is up) ---
    queue = {}
    if db_ok:
        try:
            async with async_session() as session:
                # Pending outbox rows
                r = await session.execute(
                    text("SELECT COUNT(*) FROM dispatch_outbox WHERE dispatched = false")
                )
                queue["pending_outbox"] = r.scalar()

                # Stale delivering executions (>300s old)
                stale_cutoff = now - timedelta(seconds=300)
                r = await session.execute(
                    text(
                        "SELECT COUNT(*) FROM executions "
                        "WHERE status = 'delivering' AND updated_at < :cutoff"
                    ),
                    {"cutoff": stale_cutoff},
                )
                queue["stale_executions"] = r.scalar()

                # Retries waiting
                r = await session.execute(
                    text(
                        "SELECT COUNT(*) FROM executions "
                        "WHERE status IN ('retrying', 'retry_ready')"
                    )
                )
                queue["pending_retries"] = r.scalar()

                # Pending worker claims
                r = await session.execute(
                    text(
                        "SELECT COUNT(*) FROM executions e "
                        "JOIN cues c ON e.cue_id = c.id "
                        "WHERE e.status = 'pending' "
                        "AND c.callback_transport = 'worker'"
                    )
                )
                queue["pending_worker_claims"] = r.scalar()
        except Exception:
            pass

    result = {
        "status": overall_status,
        "version": "1.0.0",
        "timestamp": now.isoformat(),
        "services": services,
    }

    if poller_info:
        result["poller"] = poller_info
    if workers_info:
        result["workers"] = workers_info
    result["queue"] = queue

    if overall_status == "healthy":
        return result
    else:
        # Still 200 for degraded so monitoring can distinguish from full outage
        return result


@router.get("/status")
async def status_check():
    """Lightweight status endpoint for uptime monitors.
    Returns healthy/degraded/down with HTTP 200 or 503.
    """
    reasons = []

    # Check PostgreSQL — query a real table to catch schema/permission issues
    try:
        async with async_session() as session:
            await session.execute(text("SELECT count(*) FROM users LIMIT 1"))
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"status": "down", "reason": "postgres unreachable"},
        )

    # Check Redis
    redis = None
    try:
        redis = await get_redis()
        await redis.ping()
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"status": "down", "reason": "redis unreachable"},
        )

    # Check poller heartbeat
    try:
        poller_last_run = await redis.get("poller:last_run")
        if poller_last_run is not None:
            last_run_dt = datetime.fromisoformat(poller_last_run)
            seconds_ago = (datetime.now(timezone.utc) - last_run_dt).total_seconds()
            if seconds_ago > 30:
                reasons.append("poller stale")
    except Exception:
        pass

    if reasons:
        return {"status": "degraded", "reason": ", ".join(reasons)}

    return {"status": "healthy"}
