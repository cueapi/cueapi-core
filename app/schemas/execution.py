from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class OutcomeDetail(BaseModel):
    success: bool
    result: Optional[str]
    error: Optional[str]
    metadata: Optional[Dict[str, Any]]
    recorded_at: datetime


class ExecutionResponse(BaseModel):
    id: str
    cue_id: str
    scheduled_for: datetime
    status: str
    http_status: Optional[int]
    attempts: int
    error_message: Optional[str]
    started_at: Optional[datetime]
    delivered_at: Optional[datetime]
    last_attempt_at: Optional[datetime]
    outcome: Optional[OutcomeDetail]
    created_at: datetime
    updated_at: datetime


class ExecutionListResponse(BaseModel):
    executions: List[ExecutionResponse]
    total: int
    limit: int
    offset: int


class ClaimableExecution(BaseModel):
    execution_id: str
    cue_id: str
    cue_name: str
    task: Optional[str] = None
    scheduled_for: datetime
    payload: Optional[Dict[str, Any]] = None
    attempt: int


class ClaimableListResponse(BaseModel):
    executions: List[ClaimableExecution]


class ClaimRequest(BaseModel):
    worker_id: str = Field(..., min_length=1, max_length=255)


class ClaimResponse(BaseModel):
    claimed: bool
    execution_id: str
    lease_seconds: int
