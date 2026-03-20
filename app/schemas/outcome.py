from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class OutcomeRequest(BaseModel):
    success: bool
    result: Optional[str] = Field(None, max_length=2000)
    error: Optional[str] = Field(None, max_length=2000)
    metadata: Optional[Dict[str, Any]] = None


class OutcomeResponse(BaseModel):
    execution_id: str
    outcome_recorded: bool
    reason: Optional[str] = None
