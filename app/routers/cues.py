from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthenticatedUser, get_current_user
from app.database import get_db
from app.schemas.cue import CueCreate, CueDetailResponse, CueListResponse, CueResponse, CueUpdate, FireRequest
from app.services.cue_service import create_cue, delete_cue, get_cue, list_cues, update_cue
from app.utils.verify_echo import apply_verify_echo

router = APIRouter(prefix="/v1/cues", tags=["cues"])


@router.post("", response_model=CueResponse, status_code=201)
async def create(
    body: CueCreate,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await create_cue(db, user, body)
    if "error" in result:
        err = result["error"]
        raise HTTPException(status_code=err["status"], detail=result)
    return result["cue"]


@router.get("", response_model=CueListResponse)
async def list_all(
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await list_cues(db, user, status=status, limit=limit, offset=offset)


@router.get("/{cue_id}", response_model=CueDetailResponse)
async def get_one(
    cue_id: str,
    execution_limit: int = Query(10, ge=1, le=100),
    execution_offset: int = Query(0, ge=0),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await get_cue(db, user, cue_id, execution_limit=execution_limit, execution_offset=execution_offset)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "cue_not_found", "message": "Cue not found", "status": 404}},
        )
    return result["cue"]


@router.patch("/{cue_id}", response_model=CueResponse)
async def update(
    cue_id: str,
    body: CueUpdate,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await update_cue(db, user, cue_id, body)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "cue_not_found", "message": "Cue not found", "status": 404}},
        )
    if "error" in result:
        err = result["error"]
        raise HTTPException(status_code=err["status"], detail=result)
    return result["cue"]


@router.delete("/{cue_id}", status_code=204)
async def delete(
    cue_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await delete_cue(db, user, cue_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "cue_not_found", "message": "Cue not found", "status": 404}},
        )
    return Response(status_code=204)


@router.post("/{cue_id}/fire", status_code=200)
async def fire_cue(
    cue_id: str,
    request: Request,
    body: Optional[FireRequest] = None,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Manually fire a cue — creates an execution immediately regardless of schedule.

    Optional body fields:
    - ``send_at`` — schedule the fire for a future UTC timestamp.
      Past timestamps treated as fire-now (forgiving fallback).
      See FireRequest for full semantics (parity port of cueapi/cueapi#618).
    """
    import uuid as uuid_mod
    from datetime import datetime, timezone
    from sqlalchemy import select
    from app.models.cue import Cue as CueModel
    from app.models.execution import Execution
    from app.models.dispatch_outbox import DispatchOutbox

    result = await db.execute(select(CueModel).where(CueModel.id == cue_id, CueModel.user_id == user.id))
    cue = result.scalar_one_or_none()
    if not cue:
        raise HTTPException(status_code=404, detail={"error": {"code": "cue_not_found", "message": "Cue not found", "status": 404}})

    now = datetime.now(timezone.utc)

    # §13 (Phase 12.1.7): per-fire scheduling. ``send_at`` in the future
    # delays dispatch until that time; past timestamps are forgiving and
    # treated as "fire now" (idempotent — caller doesn't need to worry
    # about clock skew or being a few ms late). The dispatch loop in
    # worker/poller.py:dispatch_outbox already gates on
    # ``DispatchOutbox.scheduled_at`` (NULL or <= now), so we just plumb
    # the timestamp through. ``Execution.scheduled_for`` records the
    # intended fire time for audit; the worker doesn't actually use this
    # for gating (the outbox does), but listings + dashboards show it.
    requested_at = body.send_at if body and body.send_at else None
    effective_scheduled_for = requested_at if requested_at and requested_at > now else now
    is_scheduled = effective_scheduled_for > now

    execution_id = uuid_mod.uuid4()
    execution = Execution(
        id=execution_id, cue_id=cue.id, scheduled_for=effective_scheduled_for,
        status="pending", triggered_by="manual_fire",
    )
    db.add(execution)

    if cue.callback_transport == "webhook" and cue.callback_url:
        from app.models.user import User
        user_row = await db.execute(select(User.webhook_secret).where(User.id == user.id))
        ws = user_row.scalar_one_or_none() or ""
        outbox = DispatchOutbox(
            execution_id=execution_id, cue_id=cue.id, task_type="deliver",
            # §13: when send_at is in the future, set scheduled_at so the
            # dispatcher gates dispatch until then. NULL = dispatch
            # immediately (existing behavior).
            scheduled_at=effective_scheduled_for if is_scheduled else None,
            payload={
                "execution_id": str(execution_id), "cue_id": cue.id, "cue_name": cue.name,
                "user_id": str(user.id), "callback_url": cue.callback_url,
                "callback_method": cue.callback_method, "callback_headers": cue.callback_headers or {},
                "payload": cue.payload or {}, "scheduled_for": effective_scheduled_for.isoformat(),
                "retry_max_attempts": cue.retry_max_attempts,
                "retry_backoff_minutes": cue.retry_backoff_minutes or [1, 5, 15],
                "webhook_secret": ws,
            },
        )
        db.add(outbox)

    await db.commit()
    response_content: dict = {
        "id": str(execution_id), "cue_id": cue.id,
        "scheduled_for": effective_scheduled_for.isoformat(),
        "status": "pending", "triggered_by": "manual_fire",
    }
    # BodyVerify Layer 1: opt-in echo-back when caller sets
    # X-CueAPI-Verify-Echo: true. OSS FireRequest carries only send_at
    # (datetime) — no string user-content field to echo. Helper returns
    # body_received=None + sha256 of empty bytes when header set, or {} when
    # absent. Once OSS adds a content-bearing field (e.g. payload_override
    # for parity), update this to extract the string per spec.
    response_content.update(apply_verify_echo(request=request, body_text=None))
    return response_content
