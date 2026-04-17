from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthenticatedUser, get_current_user
from app.config import settings
from app.database import get_db
from app.models.cue import Cue
from app.models.execution import Execution
from app.schemas.execution import (
    ClaimableExecution,
    ClaimableListResponse,
    ClaimRequest,
    ClaimResponse,
)
from app.schemas.outcome import OutcomeRequest, OutcomeResponse
from app.services.outcome_service import record_outcome

router = APIRouter(prefix="/v1/executions", tags=["executions"])


# ── List executions ──


@router.get("", response_model=dict)
async def list_executions(
    cue_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    outcome_state: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List executions with optional filters."""
    from sqlalchemy import func as sa_func
    from app.services.cue_service import _execution_to_response

    base = (
        select(Execution)
        .join(Cue, Execution.cue_id == Cue.id)
        .where(Cue.user_id == user.id)
    )
    count_base = (
        select(sa_func.count(Execution.id))
        .join(Cue, Execution.cue_id == Cue.id)
        .where(Cue.user_id == user.id)
    )

    if cue_id:
        base = base.where(Execution.cue_id == cue_id)
        count_base = count_base.where(Execution.cue_id == cue_id)
    if status:
        base = base.where(Execution.status == status)
        count_base = count_base.where(Execution.status == status)
    if outcome_state:
        base = base.where(Execution.outcome_state == outcome_state)
        count_base = count_base.where(Execution.outcome_state == outcome_state)

    total = await db.scalar(count_base) or 0
    result = await db.execute(
        base.order_by(Execution.created_at.desc()).limit(limit).offset(offset)
    )
    executions = result.scalars().all()

    return {
        "executions": [_execution_to_response(e).model_dump(mode="json") for e in executions],
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": (offset + limit) < total,
    }


# ── Report outcome ──


@router.post("/{execution_id}/outcome", response_model=OutcomeResponse)
async def report_outcome(
    execution_id: str,
    body: OutcomeRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await record_outcome(db, user, execution_id, body)
    if "error" in result:
        err = result["error"]
        raise HTTPException(status_code=err["status"], detail=result)
    return result["outcome"]


# ── Claimable ──


@router.get("/claimable", response_model=ClaimableListResponse)
async def get_claimable(
    task: Optional[str] = Query(None, description="Filter by payload.task"),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List pending worker-transport executions available for claiming."""
    query = (
        select(
            Execution.id,
            Execution.cue_id,
            Execution.scheduled_for,
            Execution.attempts,
            Cue.name,
            Cue.payload,
        )
        .join(Cue, Execution.cue_id == Cue.id)
        .where(
            Cue.user_id == user.id,
            Cue.callback_transport == "worker",
            Execution.status.in_(["pending", "retry_ready"]),
        )
        .order_by(Execution.scheduled_for)
        .limit(50)
    )

    result = await db.execute(query)
    rows = result.fetchall()

    executions = []
    for row in rows:
        payload = row.payload or {}
        task_name = payload.get("task")
        if task and task_name != task:
            continue
        executions.append(
            ClaimableExecution(
                execution_id=str(row.id),
                cue_id=row.cue_id,
                cue_name=row.name,
                task=task_name,
                scheduled_for=row.scheduled_for,
                payload=payload,
                attempt=row.attempts,
            )
        )

    return ClaimableListResponse(executions=executions)


# ── Get single execution ──


@router.get("/{execution_id}", response_model=dict)
async def get_execution(
    execution_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get a single execution by ID."""
    from app.services.cue_service import _execution_to_response

    result = await db.execute(
        select(Execution)
        .join(Cue, Execution.cue_id == Cue.id)
        .where(Execution.id == execution_id, Cue.user_id == user.id)
    )
    execution = result.scalar_one_or_none()
    if not execution:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "execution_not_found", "message": "Execution not found", "status": 404}},
        )
    return _execution_to_response(execution).model_dump(mode="json")


# ── Claim endpoints ──


@router.post("/claim", response_model=ClaimResponse)
async def claim_next_execution(
    body: ClaimRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Claim the next available worker-transport execution."""
    now = datetime.now(timezone.utc)
    subq = (
        select(Execution.id)
        .join(Cue, Execution.cue_id == Cue.id)
        .where(
            Cue.user_id == user.id,
            Cue.callback_transport == "worker",
            Execution.status == "pending",
        )
        .order_by(Execution.scheduled_for)
        .limit(1)
    )
    result = await db.execute(
        update(Execution)
        .where(Execution.id.in_(subq))
        .values(status="delivering", claimed_by_worker=body.worker_id, claimed_at=now, started_at=now, last_attempt_at=now, attempts=Execution.attempts + 1)
        .returning(Execution.id)
    )
    claimed = result.fetchone()
    if not claimed:
        raise HTTPException(status_code=409, detail={"error": {"code": "claim_failed", "message": "No executions available for claiming", "status": 409}})
    await db.commit()
    return ClaimResponse(claimed=True, execution_id=str(claimed[0]), lease_seconds=settings.WORKER_CLAIM_TIMEOUT_SECONDS)


@router.post("/{execution_id}/claim", response_model=ClaimResponse)
async def claim_execution(
    execution_id: str,
    body: ClaimRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Claim a specific pending worker-transport execution."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        update(Execution)
        .where(
            Execution.id == execution_id,
            Execution.status == "pending",
            Execution.cue_id.in_(select(Cue.id).where(Cue.user_id == user.id, Cue.callback_transport == "worker")),
        )
        .values(status="delivering", claimed_by_worker=body.worker_id, claimed_at=now, started_at=now, last_attempt_at=now, attempts=Execution.attempts + 1)
        .returning(Execution.id)
    )
    claimed = result.fetchone()
    if not claimed:
        raise HTTPException(status_code=409, detail={"error": {"code": "claim_failed", "message": "Execution not available for claiming", "status": 409}})
    await db.commit()
    return ClaimResponse(claimed=True, execution_id=str(claimed[0]), lease_seconds=settings.WORKER_CLAIM_TIMEOUT_SECONDS)


# ── Heartbeat ──


@router.post("/{execution_id}/heartbeat")
async def execution_heartbeat(
    execution_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    x_worker_id: Optional[str] = Header(None),
):
    """Worker heartbeat to extend claim lease during long-running jobs."""
    result = await db.execute(
        select(Execution, Cue.user_id)
        .join(Cue, Execution.cue_id == Cue.id)
        .where(Execution.id == execution_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail={"error": {"code": "execution_not_found", "message": "Execution not found", "status": 404}})

    execution, owner_id = row
    if str(owner_id) != str(user.id):
        raise HTTPException(status_code=404, detail={"error": {"code": "execution_not_found", "message": "Execution not found", "status": 404}})
    if execution.status != "delivering":
        raise HTTPException(status_code=409, detail={"error": {"code": "execution_not_claiming", "message": "Execution is not currently being claimed", "status": 409}})
    if x_worker_id and execution.claimed_by_worker and x_worker_id != execution.claimed_by_worker:
        raise HTTPException(status_code=403, detail={"error": {"code": "not_execution_owner", "message": "Only the claiming worker can heartbeat", "status": 403}})

    now = datetime.now(timezone.utc)
    execution.last_heartbeat_at = now
    execution.updated_at = now
    await db.commit()

    return {
        "execution_id": str(execution_id),
        "lease_extended_until": (now + timedelta(seconds=settings.WORKER_CLAIM_TIMEOUT_SECONDS)).isoformat(),
        "acknowledged": True,
    }


# ── Replay ──


@router.post("/{execution_id}/replay", status_code=200)
async def replay_execution(
    execution_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Replay a failed execution — creates a new pending execution for the same cue."""
    import uuid as uuid_mod

    result = await db.execute(
        select(Execution, Cue)
        .join(Cue, Execution.cue_id == Cue.id)
        .where(Execution.id == execution_id, Cue.user_id == user.id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail={"error": {"code": "execution_not_found", "message": "Execution not found", "status": 404}})

    original_exec, cue = row
    if original_exec.status not in {"success", "failed", "missed", "outcome_timeout"}:
        raise HTTPException(status_code=409, detail={"error": {"code": "execution_in_flight", "message": "Cannot replay an execution that is still in progress", "status": 409}})

    now = datetime.now(timezone.utc)
    new_exec_id = uuid_mod.uuid4()
    new_exec = Execution(id=new_exec_id, cue_id=cue.id, scheduled_for=now, status="pending", triggered_by="replay")
    db.add(new_exec)

    # For webhook transport, add to outbox
    if cue.callback_transport == "webhook" and cue.callback_url:
        from app.models.user import User
        from app.models.dispatch_outbox import DispatchOutbox
        user_row = await db.execute(select(User.webhook_secret).where(User.id == user.id))
        ws = user_row.scalar_one_or_none() or ""
        outbox = DispatchOutbox(
            execution_id=new_exec_id, cue_id=cue.id, task_type="deliver",
            payload={
                "execution_id": str(new_exec_id), "cue_id": cue.id, "cue_name": cue.name,
                "user_id": str(user.id), "callback_url": cue.callback_url,
                "callback_method": cue.callback_method, "callback_headers": cue.callback_headers or {},
                "payload": cue.payload or {}, "scheduled_for": now.isoformat(),
                "retry_max_attempts": cue.retry_max_attempts,
                "retry_backoff_minutes": cue.retry_backoff_minutes or [1, 5, 15],
                "webhook_secret": ws,
            },
        )
        db.add(outbox)

    await db.commit()
    return {"id": str(new_exec_id), "cue_id": cue.id, "scheduled_for": now.isoformat(), "status": "pending", "triggered_by": "replay", "replayed_from": str(execution_id)}


# ── Verify ──


class VerifyRequest(BaseModel):
    """Body for ``POST /v1/executions/{id}/verify``.

    Body is optional — a request with no body (or ``{}``) defaults to
    ``valid=true`` so the previous always-success behavior remains the
    default. ``valid=false`` is the new branch: it transitions to
    ``verification_failed`` and optionally persists a human-readable
    ``reason`` onto ``evidence_summary``.
    """

    valid: bool = True
    reason: Optional[str] = None


@router.post("/{execution_id}/verify")
async def verify_execution(
    execution_id: str,
    body: Optional[VerifyRequest] = Body(None),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mark execution outcome as verified or verification-failed.

    Accepts ``{valid: bool, reason: str?}``. ``valid=true`` (default)
    transitions to ``verified_success``; ``valid=false`` transitions to
    ``verification_failed`` and records the reason on
    ``evidence_summary`` (truncated to 500 chars). Accepted starting
    states: ``reported_success``, ``reported_failure``,
    ``verification_pending``.
    """
    result = await db.execute(
        select(Execution).join(Cue, Execution.cue_id == Cue.id)
        .where(Execution.id == execution_id, Cue.user_id == user.id)
    )
    execution = result.scalar_one_or_none()
    if not execution:
        raise HTTPException(status_code=404, detail={"error": {"code": "execution_not_found", "message": "Execution not found", "status": 404}})

    if execution.outcome_state not in {
        "reported_success",
        "reported_failure",
        "verification_pending",
    }:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "code": "invalid_state",
                    "message": f"Cannot verify from state '{execution.outcome_state}'",
                    "status": 409,
                }
            },
        )

    payload = body or VerifyRequest()
    now = datetime.now(timezone.utc)
    if payload.valid:
        execution.outcome_state = "verified_success"
        execution.evidence_validation_state = "valid"
    else:
        execution.outcome_state = "verification_failed"
        execution.evidence_validation_state = "invalid"
        if payload.reason:
            # Persist reason alongside any existing summary; truncate
            # to the column cap. We prepend so operators who set a
            # summary at outcome-report time still see it.
            truncated = payload.reason[:500]
            if execution.evidence_summary:
                combined = f"{execution.evidence_summary} | verification rejected: {truncated}"
                execution.evidence_summary = combined[:500]
            else:
                execution.evidence_summary = truncated
    execution.updated_at = now
    await db.commit()
    return {
        "execution_id": str(execution_id),
        "outcome_state": execution.outcome_state,
        "valid": payload.valid,
        "reason": payload.reason,
    }


# ── Verification pending ──


@router.post("/{execution_id}/verification-pending")
async def mark_verification_pending(
    execution_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mark execution as pending verification."""
    result = await db.execute(
        select(Execution).join(Cue, Execution.cue_id == Cue.id)
        .where(Execution.id == execution_id, Cue.user_id == user.id)
    )
    execution = result.scalar_one_or_none()
    if not execution:
        raise HTTPException(status_code=404)

    if not execution.outcome_recorded_at:
        raise HTTPException(status_code=409, detail={"error": {"code": "no_outcome", "message": "No outcome recorded yet"}})

    execution.outcome_state = "verification_pending"
    execution.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return {"execution_id": str(execution_id), "outcome_state": "verification_pending"}


# ── Evidence ──


@router.patch("/{execution_id}/evidence")
async def append_evidence(
    execution_id: str,
    body: dict,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Append evidence to an execution after outcome is recorded."""
    result = await db.execute(
        select(Execution)
        .join(Cue, Execution.cue_id == Cue.id)
        .where(Execution.id == execution_id, Cue.user_id == user.id)
    )
    execution = result.scalar_one_or_none()
    if not execution:
        raise HTTPException(status_code=404, detail={"error": {"code": "execution_not_found", "message": "Execution not found", "status": 404}})
    if execution.outcome_recorded_at is None:
        raise HTTPException(status_code=409, detail={"error": {"code": "no_outcome", "message": "Report outcome first before appending evidence", "status": 409}})

    now = datetime.now(timezone.utc)
    if body.get("external_id"):
        execution.evidence_external_id = body["external_id"]
    if body.get("result_url"):
        execution.evidence_result_url = str(body["result_url"])
    if body.get("result_ref"):
        execution.evidence_result_ref = body["result_ref"]
    if body.get("result_type"):
        execution.evidence_result_type = body["result_type"]
    if body.get("summary"):
        execution.evidence_summary = str(body["summary"])[:500]
    if body.get("artifacts"):
        execution.evidence_artifacts = body["artifacts"]
    if body.get("metadata"):
        execution.evidence_metadata = body["metadata"]

    execution.updated_at = now
    await db.commit()
    return {"execution_id": str(execution_id), "outcome_state": execution.outcome_state, "evidence_updated": True}
