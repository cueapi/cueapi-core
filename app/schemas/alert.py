from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class AlertResponse(BaseModel):
    id: str
    cue_id: Optional[str] = None
    execution_id: Optional[str] = None
    alert_type: str
    severity: str
    message: str
    metadata: Optional[Dict[str, Any]] = None
    acknowledged: bool
    created_at: datetime


class AlertListResponse(BaseModel):
    alerts: List[AlertResponse]
    total: int
    limit: int
    offset: int
