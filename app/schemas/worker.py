from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class HeartbeatRequest(BaseModel):
    worker_id: str = Field(..., max_length=255)
    handlers: Optional[List[str]] = None


class HeartbeatResponse(BaseModel):
    acknowledged: bool
    server_time: datetime
