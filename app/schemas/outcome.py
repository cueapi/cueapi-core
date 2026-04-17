from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, HttpUrl


class OutcomeRequest(BaseModel):
    """Outcome report. Evidence fields are optional and additive — a
    caller that only sends {success, result, error, metadata} gets the
    identical behavior it always did. Evidence fields feed the
    verification-modes policy configured on the cue (see
    ``VerificationMode``)."""

    success: bool
    result: Optional[str] = Field(None, max_length=2000)
    error: Optional[str] = Field(None, max_length=2000)
    metadata: Optional[Dict[str, Any]] = None
    # Evidence fields — recorded on the Execution's ``evidence_*``
    # columns. Any missing evidence required by the cue's verification
    # mode causes the outcome to land in ``verification_failed`` rather
    # than ``reported_success``.
    external_id: Optional[str] = Field(None, max_length=500)
    result_url: Optional[HttpUrl] = None
    result_ref: Optional[str] = Field(None, max_length=500)
    result_type: Optional[str] = Field(None, max_length=100)
    summary: Optional[str] = Field(None, max_length=500)
    artifacts: Optional[List[Any]] = None


class OutcomeResponse(BaseModel):
    execution_id: str
    outcome_recorded: bool
    outcome_state: Optional[str] = None
    reason: Optional[str] = None
