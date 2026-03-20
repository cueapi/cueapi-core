from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthenticatedUser, get_current_user
from app.database import get_db
from app.schemas.cue import CueCreate, CueDetailResponse, CueListResponse, CueResponse, CueUpdate
from app.services.cue_service import create_cue, delete_cue, get_cue, list_cues, update_cue

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
