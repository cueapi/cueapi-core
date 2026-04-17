from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from app.schemas.execution import ExecutionResponse


class VerificationMode(str, Enum):
    """Outcome-verification policy for a cue.

    - ``none``: the reported ``success`` bool is final. Default.
    - ``require_external_id``/``result_url``/``artifacts``: evidence
      field must be present on the outcome report; if missing, the
      execution is marked ``verification_failed``. If present and
      ``success=True``, the execution is marked ``verified_success``.
    - ``manual``: every successful outcome sits in
      ``verification_pending`` until someone calls
      ``POST /v1/executions/{id}/verify``.
    """

    none = "none"
    require_external_id = "require_external_id"
    require_result_url = "require_result_url"
    require_artifacts = "require_artifacts"
    manual = "manual"


class VerificationPolicy(BaseModel):
    """Outcome-verification policy. Only ``mode`` today; kept as a
    sub-object so future fields (e.g. ``auto_verify_after``) can be
    added without breaking the API shape."""

    mode: VerificationMode = Field(default=VerificationMode.none)


class ScheduleCreate(BaseModel):
    type: str  # "once" | "recurring"
    cron: Optional[str] = None
    at: Optional[datetime] = None
    timezone: str = "UTC"


class CallbackCreate(BaseModel):
    url: Optional[HttpUrl] = None
    method: str = "POST"
    headers: Optional[Dict[str, str]] = None
    transport: Optional[str] = None


class RetryConfig(BaseModel):
    max_attempts: int = Field(default=3, ge=1, le=10)
    backoff_minutes: List[int] = Field(default=[1, 5, 15])


class OnFailureConfig(BaseModel):
    """Failure escalation configuration."""
    email: bool = True
    webhook: Optional[str] = None
    pause: bool = False


class CueCreate(BaseModel):
    name: str = Field(..., max_length=255)
    description: Optional[str] = None
    schedule: ScheduleCreate
    callback: Optional[CallbackCreate] = None
    transport: str = "webhook"
    payload: Optional[dict] = Field(default={})
    retry: Optional[RetryConfig] = Field(default_factory=RetryConfig)
    on_failure: Optional[OnFailureConfig] = Field(default_factory=OnFailureConfig)
    verification: Optional[VerificationPolicy] = None

    @model_validator(mode="after")
    def validate_transport(self) -> "CueCreate":
        # Reject transport inside callback — must be top-level only
        if self.callback and self.callback.transport:
            raise ValueError("transport must be specified at the top level, not inside callback")

        if self.transport not in ("webhook", "worker"):
            raise ValueError("transport must be 'webhook' or 'worker'")
        if self.transport == "webhook":
            if self.callback is None or self.callback.url is None:
                raise ValueError("callback.url is required for webhook transport")
        elif self.transport == "worker":
            # Worker cues don't need a callback URL; default callback if not provided
            if self.callback is None:
                self.callback = CallbackCreate(transport="worker")
        return self


class CueUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None  # "active" | "paused"
    schedule: Optional[ScheduleCreate] = None
    callback: Optional[CallbackCreate] = None
    payload: Optional[dict] = None
    retry: Optional[RetryConfig] = None
    on_failure: Optional[OnFailureConfig] = None
    verification: Optional[VerificationPolicy] = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v is not None and v not in ("active", "paused"):
            raise ValueError("status must be 'active' or 'paused'")
        return v


class CueResponse(BaseModel):
    id: str
    name: str
    description: Optional[str]
    status: str
    transport: str = "webhook"
    schedule: dict
    callback: dict
    payload: dict
    retry: dict
    next_run: Optional[datetime]
    last_run: Optional[datetime]
    run_count: int
    fired_count: int = 0
    on_failure: Optional[dict] = None
    verification: Optional[dict] = None
    warning: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class CueDetailResponse(CueResponse):
    executions: List[ExecutionResponse] = []
    execution_total: int = 0
    execution_limit: int = 10
    execution_offset: int = 0


class CueListResponse(BaseModel):
    cues: List[CueResponse]
    total: int
    limit: int
    offset: int
