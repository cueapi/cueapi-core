from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthenticatedUser, get_current_user
from app.database import get_db
from app.schemas.alert import AlertListResponse, AlertResponse
from app.services.alert_service import list_alerts

router = APIRouter(prefix="/v1/alerts", tags=["alerts"])

_VALID_TYPES = {"outcome_timeout", "verification_failed", "consecutive_failures"}


@router.get("", response_model=AlertListResponse)
async def get_alerts(
    alert_type: Optional[str] = Query(None, description="Filter by alert type"),
    since: Optional[datetime] = Query(None, description="Return alerts created at or after this ISO timestamp"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List alerts for the authenticated user."""
    if alert_type and alert_type not in _VALID_TYPES:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "code": "invalid_filter",
                    "message": f"alert_type must be one of: {', '.join(sorted(_VALID_TYPES))}",
                    "status": 400,
                }
            },
        )
    result = await list_alerts(
        db, user.id, alert_type=alert_type, since=since, limit=limit, offset=offset
    )
    return AlertListResponse(
        alerts=[
            AlertResponse(
                id=str(a.id),
                cue_id=a.cue_id,
                execution_id=str(a.execution_id) if a.execution_id else None,
                alert_type=a.alert_type,
                severity=a.severity,
                message=a.message,
                metadata=a.alert_metadata,
                acknowledged=a.acknowledged,
                created_at=a.created_at,
            )
            for a in result["alerts"]
        ],
        total=result["total"],
        limit=result["limit"],
        offset=result["offset"],
    )
