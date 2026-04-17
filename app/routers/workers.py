from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthenticatedUser, get_current_user
from app.database import get_db
from app.models.worker import Worker
from app.schemas.worker import HeartbeatRequest, HeartbeatResponse

router = APIRouter(prefix="/v1/worker", tags=["worker"])


class WorkerInfo(BaseModel):
    worker_id: str
    last_heartbeat: datetime
    seconds_since_heartbeat: int
    heartbeat_status: str  # active, stale, dead
    handlers: Optional[List[str]] = None
    registered_since: datetime


class WorkerListResponse(BaseModel):
    workers: List[WorkerInfo]


@router.post("/heartbeat", response_model=HeartbeatResponse)
async def heartbeat(
    body: HeartbeatRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """UPSERT worker: insert or update on (user_id, worker_id) conflict."""
    now = datetime.now(timezone.utc)

    stmt = (
        pg_insert(Worker)
        .values(
            user_id=user.id,
            worker_id=body.worker_id,
            handlers=body.handlers,
            last_heartbeat=now,
        )
        .on_conflict_do_update(
            constraint="unique_user_worker",
            set_={
                "handlers": body.handlers,
                "last_heartbeat": now,
            },
        )
    )
    await db.execute(stmt)
    await db.commit()

    return HeartbeatResponse(acknowledged=True, server_time=now)


workers_list_router = APIRouter(prefix="/v1/workers", tags=["worker"])


@workers_list_router.get("", response_model=WorkerListResponse)
async def list_workers(
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all registered workers with heartbeat status."""
    result = await db.execute(
        select(Worker)
        .where(Worker.user_id == user.id)
        .order_by(Worker.last_heartbeat.desc())
    )
    workers = result.scalars().all()

    now = datetime.now(timezone.utc)
    worker_list = []
    for w in workers:
        seconds_ago = int((now - w.last_heartbeat).total_seconds())
        if seconds_ago < 180:
            status = "active"
        elif seconds_ago < 900:
            status = "stale"
        else:
            status = "dead"

        worker_list.append(WorkerInfo(
            worker_id=w.worker_id,
            last_heartbeat=w.last_heartbeat,
            seconds_since_heartbeat=seconds_ago,
            heartbeat_status=status,
            handlers=w.handlers,
            registered_since=w.created_at,
        ))

    return WorkerListResponse(workers=worker_list)


@workers_list_router.delete("/{worker_id}", status_code=204)
async def delete_worker(
    worker_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a registered worker. Only the owning user can delete their workers."""
    from sqlalchemy import delete as sa_delete

    result = await db.execute(
        sa_delete(Worker).where(
            Worker.user_id == user.id,
            Worker.worker_id == worker_id,
        ).returning(Worker.id)
    )
    deleted = result.fetchone()
    if not deleted:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "worker_not_found", "message": "Worker not found", "status": 404}},
        )
    await db.commit()
