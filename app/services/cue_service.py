from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytz
from croniter import croniter
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthenticatedUser
from app.config import settings
from app.models.cue import Cue
from app.models.execution import Execution
from app.models.worker import Worker
from app.schemas.cue import CueCreate, CueDetailResponse, CueResponse, CueUpdate
from app.schemas.execution import ExecutionResponse, OutcomeDetail
from app.utils.ids import generate_cue_id
from app.utils.url_validation import validate_callback_url


def validate_cron(expression: str) -> bool:
    try:
        croniter(expression)
        return True
    except (ValueError, KeyError):
        return False


def _contains_null_byte(obj) -> bool:
    """Recursively check if any string in a dict/list contains a null byte."""
    if isinstance(obj, str):
        return "\x00" in obj
    if isinstance(obj, dict):
        return any(_contains_null_byte(k) or _contains_null_byte(v) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return any(_contains_null_byte(item) for item in obj)
    return False


def validate_timezone(timezone_str: str) -> bool:
    """Check if a timezone string is valid."""
    try:
        pytz.timezone(timezone_str)
        return True
    except pytz.exceptions.UnknownTimeZoneError:
        return False


def get_next_run(expression: str, timezone_str: str = "UTC", after: Optional[datetime] = None) -> datetime:
    tz = pytz.timezone(timezone_str)
    base = after or datetime.now(tz)
    if base.tzinfo is None:
        base = tz.localize(base)
    else:
        base = base.astimezone(tz)
    cron = croniter(expression, base)
    return cron.get_next(datetime).astimezone(pytz.utc)


def _cue_to_response(cue: Cue) -> CueResponse:
    schedule = {
        "type": cue.schedule_type,
        "timezone": cue.schedule_timezone,
    }
    if cue.schedule_cron:
        schedule["cron"] = cue.schedule_cron
    if cue.schedule_at:
        schedule["at"] = cue.schedule_at.isoformat()

    callback = {
        "url": cue.callback_url,
        "method": cue.callback_method,
        "headers": cue.callback_headers or {},
    }

    retry = {
        "max_attempts": cue.retry_max_attempts,
        "backoff_minutes": cue.retry_backoff_minutes,
    }

    return CueResponse(
        id=cue.id,
        name=cue.name,
        description=cue.description,
        status=cue.status,
        transport=cue.callback_transport or "webhook",
        schedule=schedule,
        callback=callback,
        payload=cue.payload or {},
        retry=retry,
        next_run=cue.next_run,
        last_run=cue.last_run,
        run_count=cue.run_count,
        fired_count=getattr(cue, 'fired_count', 0) or 0,
        on_failure=getattr(cue, 'on_failure', None),
        created_at=cue.created_at,
        updated_at=cue.updated_at,
    )


def _execution_to_response(ex: Execution) -> ExecutionResponse:
    outcome = None
    if ex.outcome_recorded_at is not None:
        outcome = OutcomeDetail(
            success=ex.outcome_success,
            result=ex.outcome_result,
            error=ex.outcome_error,
            metadata=ex.outcome_metadata,
            recorded_at=ex.outcome_recorded_at,
        )

    return ExecutionResponse(
        id=str(ex.id),
        cue_id=ex.cue_id,
        scheduled_for=ex.scheduled_for,
        status=ex.status,
        http_status=ex.http_status,
        attempts=ex.attempts,
        error_message=ex.error_message,
        started_at=ex.started_at,
        delivered_at=ex.delivered_at,
        last_attempt_at=ex.last_attempt_at,
        outcome=outcome,
        created_at=ex.created_at,
        updated_at=ex.updated_at,
    )


async def create_cue(db: AsyncSession, user: AuthenticatedUser, data: CueCreate) -> dict:
    # Check duplicate cue name
    dup_result = await db.execute(
        select(func.count())
        .select_from(Cue)
        .where(Cue.user_id == user.id, Cue.name == data.name)
    )
    if dup_result.scalar() > 0:
        return {
            "error": {"code": "duplicate_cue_name", "message": f"A cue named '{data.name}' already exists", "status": 409}
        }

    # Check cue limit
    count_result = await db.execute(
        select(func.count())
        .select_from(Cue)
        .where(Cue.user_id == user.id, Cue.status.in_(["active", "paused"]))
    )
    active_count = count_result.scalar()
    if active_count >= user.active_cue_limit:
        return {
            "error": {"code": "cue_limit_exceeded", "message": f"Active cue limit of {user.active_cue_limit} reached", "status": 403}
        }

    # Validate callback URL (SSRF protection) — skip for worker transport
    transport = data.transport or "webhook"
    warning = None

    if transport == "webhook":
        is_valid, error_msg = validate_callback_url(str(data.callback.url), settings.ENV)
        if not is_valid:
            return {
                "error": {"code": "invalid_callback_url", "message": error_msg, "status": 400}
            }
    elif transport == "worker":
        # Check if user has active workers, add warning if not
        heartbeat_cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=settings.WORKER_HEARTBEAT_TIMEOUT_SECONDS
        )
        worker_result = await db.execute(
            select(func.count())
            .select_from(Worker)
            .where(
                Worker.user_id == user.id,
                Worker.last_heartbeat >= heartbeat_cutoff,
            )
        )
        active_workers = worker_result.scalar() or 0
        if active_workers == 0:
            warning = "No active workers found. Start a cueapi-worker to process this cue."

    # Validate payload
    payload = data.payload or {}
    try:
        payload_json = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        return {
            "error": {"code": "invalid_payload", "message": f"Payload is not serializable: {e}", "status": 400}
        }
    # Reject NULL bytes (PostgreSQL JSONB does not support them).
    # Check the raw Python object because json.dumps escapes \x00 to \u0000.
    if _contains_null_byte(payload):
        return {
            "error": {"code": "invalid_payload", "message": "Payload contains null bytes which are not supported", "status": 400}
        }
    payload_size = len(payload_json.encode("utf-8"))
    if payload_size > 1_048_576:
        return {
            "error": {"code": "invalid_payload_size", "message": "Payload must be under 1MB", "status": 400}
        }

    # Validate timezone
    if not validate_timezone(data.schedule.timezone):
        return {
            "error": {"code": "invalid_timezone", "message": f"Unknown timezone: '{data.schedule.timezone}'", "status": 422}
        }

    # Validate schedule and calculate next_run
    next_run = None
    if data.schedule.type == "recurring":
        if not data.schedule.cron:
            return {
                "error": {"code": "invalid_schedule", "message": "Cron expression is required for recurring schedules", "status": 400}
            }
        if not validate_cron(data.schedule.cron):
            return {
                "error": {"code": "invalid_schedule", "message": "Invalid cron expression", "status": 400}
            }
        next_run = get_next_run(data.schedule.cron, data.schedule.timezone)
    elif data.schedule.type == "once":
        if not data.schedule.at:
            return {
                "error": {"code": "invalid_schedule", "message": "Timestamp is required for one-time schedules", "status": 400}
            }
        schedule_at = data.schedule.at
        if schedule_at.tzinfo is None:
            schedule_at = schedule_at.replace(tzinfo=timezone.utc)
        if schedule_at <= datetime.now(timezone.utc):
            return {
                "error": {"code": "invalid_schedule", "message": "Scheduled time must be in the future", "status": 400}
            }
        next_run = schedule_at
    else:
        return {
            "error": {"code": "invalid_schedule", "message": "Schedule type must be 'once' or 'recurring'", "status": 400}
        }

    retry = data.retry or CueCreate.model_fields["retry"].default_factory()

    # Validate on_failure webhook URL (SSRF protection)
    on_failure = data.on_failure
    on_failure_dict = None
    if on_failure:
        on_failure_dict = {"email": on_failure.email, "webhook": on_failure.webhook, "pause": on_failure.pause}
        if on_failure.webhook:
            is_valid, error_msg = validate_callback_url(on_failure.webhook, settings.ENV)
            if not is_valid:
                return {
                    "error": {"code": "invalid_callback_url", "message": f"on_failure.webhook: {error_msg}", "status": 400}
                }
    else:
        on_failure_dict = {"email": True, "webhook": None, "pause": False}

    cue = Cue(
        id=generate_cue_id(),
        user_id=user.id,
        name=data.name,
        description=data.description,
        status="active",
        schedule_type=data.schedule.type,
        schedule_cron=data.schedule.cron,
        schedule_at=data.schedule.at if data.schedule.type == "once" else None,
        schedule_timezone=data.schedule.timezone,
        callback_url=str(data.callback.url) if data.callback and data.callback.url else None,
        callback_method=data.callback.method if data.callback else "POST",
        callback_headers=data.callback.headers or {} if data.callback else {},
        callback_transport=transport,
        payload=payload,
        retry_max_attempts=retry.max_attempts,
        retry_backoff_minutes=retry.backoff_minutes,
        next_run=next_run,
        on_failure=on_failure_dict,
    )

    db.add(cue)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return {
            "error": {"code": "duplicate_cue_name", "message": f"A cue named '{data.name}' already exists", "status": 409}
        }
    await db.refresh(cue)

    resp = _cue_to_response(cue)
    if warning:
        resp.warning = warning
    return {"cue": resp}


async def list_cues(
    db: AsyncSession, user: AuthenticatedUser, status: Optional[str] = None, limit: int = 50, offset: int = 0
) -> dict:
    query = select(Cue).where(Cue.user_id == user.id)
    count_query = select(func.count()).select_from(Cue).where(Cue.user_id == user.id)

    if status:
        query = query.where(Cue.status == status)
        count_query = count_query.where(Cue.status == status)

    total_result = await db.execute(count_query)
    total = total_result.scalar()

    query = query.order_by(Cue.created_at.desc()).limit(limit).offset(offset)
    result = await db.execute(query)
    cues = result.scalars().all()

    return {
        "cues": [_cue_to_response(c) for c in cues],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


async def get_cue(db: AsyncSession, user: AuthenticatedUser, cue_id: str, execution_limit: int = 10, execution_offset: int = 0) -> Optional[dict]:
    result = await db.execute(select(Cue).where(Cue.id == cue_id, Cue.user_id == user.id))
    cue = result.scalar_one_or_none()
    if cue is None:
        return None

    # Count total executions
    total_result = await db.execute(
        select(func.count()).select_from(Execution).where(Execution.cue_id == cue_id)
    )
    execution_total = total_result.scalar()

    # Fetch paginated executions
    exec_result = await db.execute(
        select(Execution)
        .where(Execution.cue_id == cue_id)
        .order_by(Execution.created_at.desc())
        .limit(execution_limit)
        .offset(execution_offset)
    )
    executions = exec_result.scalars().all()

    cue_resp = _cue_to_response(cue)
    detail = CueDetailResponse(
        **cue_resp.model_dump(),
        executions=[_execution_to_response(e) for e in executions],
        execution_total=execution_total,
        execution_limit=execution_limit,
        execution_offset=execution_offset,
    )
    return {"cue": detail}


async def update_cue(db: AsyncSession, user: AuthenticatedUser, cue_id: str, data: CueUpdate) -> Optional[dict]:
    result = await db.execute(select(Cue).where(Cue.id == cue_id, Cue.user_id == user.id))
    cue = result.scalar_one_or_none()
    if cue is None:
        return None

    if data.name is not None:
        cue.name = data.name
    if data.description is not None:
        cue.description = data.description

    if data.callback is not None:
        # Only validate SSRF for webhook transport
        if cue.callback_transport == "webhook" and data.callback.url is not None:
            is_valid, error_msg = validate_callback_url(str(data.callback.url), settings.ENV)
            if not is_valid:
                return {
                    "error": {"code": "invalid_callback_url", "message": error_msg, "status": 400}
                }
        if data.callback.url is not None:
            cue.callback_url = str(data.callback.url)
        cue.callback_method = data.callback.method
        cue.callback_headers = data.callback.headers or {}

    if data.payload is not None:
        try:
            payload_json = json.dumps(data.payload, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            return {
                "error": {"code": "invalid_payload", "message": f"Payload is not serializable: {e}", "status": 400}
            }
        if _contains_null_byte(data.payload):
            return {
                "error": {"code": "invalid_payload", "message": "Payload contains null bytes which are not supported", "status": 400}
            }
        payload_size = len(payload_json.encode("utf-8"))
        if payload_size > 1_048_576:
            return {
                "error": {"code": "invalid_payload_size", "message": "Payload must be under 1MB", "status": 400}
            }
        cue.payload = data.payload

    if data.retry is not None:
        cue.retry_max_attempts = data.retry.max_attempts
        cue.retry_backoff_minutes = data.retry.backoff_minutes

    if data.on_failure is not None:
        if data.on_failure.webhook:
            is_valid, error_msg = validate_callback_url(data.on_failure.webhook, settings.ENV)
            if not is_valid:
                return {
                    "error": {"code": "invalid_callback_url", "message": f"on_failure.webhook: {error_msg}", "status": 400}
                }
        cue.on_failure = {
            "email": data.on_failure.email,
            "webhook": data.on_failure.webhook,
            "pause": data.on_failure.pause,
        }

    if data.schedule is not None:
        # Validate timezone
        if not validate_timezone(data.schedule.timezone):
            return {
                "error": {"code": "invalid_timezone", "message": f"Unknown timezone: '{data.schedule.timezone}'", "status": 422}
            }
        if data.schedule.type == "recurring":
            if not data.schedule.cron:
                return {
                    "error": {"code": "invalid_schedule", "message": "Cron expression is required for recurring schedules", "status": 400}
                }
            if not validate_cron(data.schedule.cron):
                return {
                    "error": {"code": "invalid_schedule", "message": "Invalid cron expression", "status": 400}
                }
            cue.schedule_type = "recurring"
            cue.schedule_cron = data.schedule.cron
            cue.schedule_at = None
            cue.schedule_timezone = data.schedule.timezone
            cue.next_run = get_next_run(data.schedule.cron, data.schedule.timezone)
        elif data.schedule.type == "once":
            if not data.schedule.at:
                return {
                    "error": {"code": "invalid_schedule", "message": "Timestamp is required for one-time schedules", "status": 400}
                }
            schedule_at = data.schedule.at
            if schedule_at.tzinfo is None:
                schedule_at = schedule_at.replace(tzinfo=timezone.utc)
            if schedule_at <= datetime.now(timezone.utc):
                return {
                    "error": {"code": "invalid_schedule", "message": "Scheduled time must be in the future", "status": 400}
                }
            cue.schedule_type = "once"
            cue.schedule_cron = None
            cue.schedule_at = schedule_at
            cue.schedule_timezone = data.schedule.timezone
            cue.next_run = schedule_at

    # Handle status changes (after schedule, since resume needs schedule info)
    if data.status is not None:
        if data.status == "paused":
            cue.status = "paused"
            cue.next_run = None
        elif data.status == "active":
            cue.status = "active"
            # Recalculate next_run
            if cue.schedule_type == "recurring" and cue.schedule_cron:
                cue.next_run = get_next_run(cue.schedule_cron, cue.schedule_timezone)
            elif cue.schedule_type == "once" and cue.schedule_at:
                cue.next_run = cue.schedule_at

    cue.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(cue)

    return {"cue": _cue_to_response(cue)}


async def delete_cue(db: AsyncSession, user: AuthenticatedUser, cue_id: str) -> Optional[bool]:
    result = await db.execute(select(Cue).where(Cue.id == cue_id, Cue.user_id == user.id))
    cue = result.scalar_one_or_none()
    if cue is None:
        return None
    await db.delete(cue)
    await db.commit()
    return True
