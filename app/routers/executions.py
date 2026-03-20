from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
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
            Execution.status == "pending",
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

        # Filter by task if specified
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


@router.post("/claim", response_model=ClaimResponse)
async def claim_next_execution(
    body: ClaimRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Claim the next available worker-transport execution.

    Picks the oldest pending execution belonging to the user's worker cues.
    Uses conditional UPDATE WHERE status='pending' to prevent race conditions.
    Returns 409 if no executions are available for claiming.
    """
    now = datetime.now(timezone.utc)

    # Find oldest pending execution for this user's worker cues
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
        .values(
            status="delivering",
            claimed_by_worker=body.worker_id,
            claimed_at=now,
            started_at=now,
            last_attempt_at=now,
            attempts=Execution.attempts + 1,
        )
        .returning(Execution.id)
    )
    claimed = result.fetchone()

    if not claimed:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "code": "claim_failed",
                    "message": "No executions available for claiming",
                    "status": 409,
                }
            },
        )

    await db.commit()

    return ClaimResponse(
        claimed=True,
        execution_id=str(claimed[0]),
        lease_seconds=settings.WORKER_CLAIM_TIMEOUT_SECONDS,
    )


@router.post("/{execution_id}/claim", response_model=ClaimResponse)
async def claim_execution(
    execution_id: str,
    body: ClaimRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Claim a specific pending worker-transport execution for processing.

    Uses conditional UPDATE WHERE status='pending' to prevent race conditions.
    Returns 409 if already claimed or not eligible.
    """
    now = datetime.now(timezone.utc)

    # Conditional UPDATE: only claim if pending AND belongs to this user AND is worker transport
    result = await db.execute(
        update(Execution)
        .where(
            Execution.id == execution_id,
            Execution.status == "pending",
            Execution.cue_id.in_(
                select(Cue.id).where(
                    Cue.user_id == user.id,
                    Cue.callback_transport == "worker",
                )
            ),
        )
        .values(
            status="delivering",
            claimed_by_worker=body.worker_id,
            claimed_at=now,
            started_at=now,
            last_attempt_at=now,
            attempts=Execution.attempts + 1,
        )
        .returning(Execution.id)
    )
    claimed = result.fetchone()

    if not claimed:
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "code": "claim_failed",
                    "message": "Execution not available for claiming (already claimed, wrong user, or not worker transport)",
                    "status": 409,
                }
            },
        )

    await db.commit()

    return ClaimResponse(
        claimed=True,
        execution_id=str(claimed[0]),
        lease_seconds=settings.WORKER_CLAIM_TIMEOUT_SECONDS,
    )
