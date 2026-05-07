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


class LiveClaimRequest(BaseModel):
    """Body for ``POST /v1/executions/{id}/live-claim``.

    A Live session's claim-watcher hits this endpoint at atomic-mv
    time when it wins the claim race; immediately afterwards it POSTs
    here with the agent's ``session_token`` (an opaque identifier
    minted at attach time). The server records the attestation on
    the execution row. The token is stored as-is and not currently
    validated against an attach-records table — that's a future
    hardening once the attach-record primitive lands. For now the
    trust model is "if you have an API key for this user and you
    POST here, the attestation is yours."
    """

    session_token: str = Field(..., min_length=8, max_length=128)


class LiveClaimResponse(BaseModel):
    attested: bool
    execution_id: str
    live_claimed_at: datetime
