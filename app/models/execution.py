from __future__ import annotations

import uuid

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.database import Base


class Execution(Base):
    __tablename__ = "executions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cue_id = Column(String(20), nullable=False, index=True)
    scheduled_for = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(20), nullable=False, default="pending")
    http_status = Column(Integer)
    response_body = Column(Text)
    attempts = Column(Integer, nullable=False, default=0)
    next_retry = Column(DateTime(timezone=True))
    error_message = Column(Text)
    started_at = Column(DateTime(timezone=True))
    delivered_at = Column(DateTime(timezone=True))
    last_attempt_at = Column(DateTime(timezone=True))
    claimed_by_worker = Column(String(255), nullable=True)
    claimed_at = Column(DateTime(timezone=True), nullable=True)
    outcome_success = Column(Boolean, nullable=True)
    outcome_result = Column(Text, nullable=True)
    outcome_error = Column(Text, nullable=True)
    outcome_metadata = Column(JSONB, nullable=True)
    outcome_recorded_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'delivering', 'success', 'failed', 'retrying', 'retry_ready', 'missed')",
            name="valid_exec_status",
        ),
        UniqueConstraint("cue_id", "scheduled_for", name="idx_executions_dedup"),
    )
