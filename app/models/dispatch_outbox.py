from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.database import Base


class DispatchOutbox(Base):
    __tablename__ = "dispatch_outbox"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Nullable since migration 021: cue-task rows still set execution_id +
    # cue_id; message-task rows (deliver_message / retry_message) leave
    # them NULL and reference message_id in the payload instead.
    execution_id = Column(UUID(as_uuid=True), nullable=True)
    cue_id = Column(String(20), nullable=True)
    task_type = Column(String(20), nullable=False, default="deliver")
    payload = Column(JSONB, nullable=False, default={})
    dispatched = Column(Boolean, nullable=False, default=False)
    dispatch_attempts = Column(Integer, nullable=False, default=0)
    last_dispatch_error = Column(Text)
    # Slice 3b (migration 022): NULL = dispatch immediately. Set on
    # retry_message rows to defer dispatch until backoff elapses.
    scheduled_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "task_type IN ('deliver', 'retry', 'deliver_message', 'retry_message')",
            name="valid_task_type",
        ),
        CheckConstraint(
            """
            (task_type IN ('deliver', 'retry') AND execution_id IS NOT NULL)
            OR
            (task_type IN ('deliver_message', 'retry_message') AND payload ? 'message_id')
            """,
            name="task_payload_shape",
        ),
    )
