from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.database import Base


class DispatchOutbox(Base):
    __tablename__ = "dispatch_outbox"

    id = Column(Integer, primary_key=True, autoincrement=True)
    execution_id = Column(UUID(as_uuid=True), nullable=False)
    cue_id = Column(String(20), nullable=False)
    task_type = Column(String(20), nullable=False, default="deliver")
    payload = Column(JSONB, nullable=False, default={})
    dispatched = Column(Boolean, nullable=False, default=False)
    dispatch_attempts = Column(Integer, nullable=False, default=0)
    last_dispatch_error = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("task_type IN ('deliver', 'retry')", name="valid_task_type"),
    )
