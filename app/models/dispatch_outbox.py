from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Column, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.database import Base


class DispatchOutbox(Base):
    __tablename__ = "dispatch_outbox"

    # Bigint id post-migration-021: allows for the higher row volume
    # introduced by message-task rows (one per outbound delivery).
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    # NULLABLE post-migration-021 to support message-task rows. Cue-task
    # rows ('deliver' / 'retry') still always have ``execution_id`` and
    # ``cue_id`` populated; message-task rows
    # ('deliver_message' / 'retry_message') have NULL here and reference
    # ``message_id`` in ``payload``. Discrimination enforced by the
    # ``task_payload_shape`` check constraint added in migration 021.
    execution_id = Column(
        UUID(as_uuid=True),
        ForeignKey("executions.id", ondelete="CASCADE"),
        nullable=True,
    )
    cue_id = Column(String(20), nullable=True)
    task_type = Column(String(20), nullable=False, default="deliver")
    payload = Column(JSONB, nullable=False, default={})
    dispatched = Column(Boolean, nullable=False, default=False)
    dispatch_attempts = Column(Integer, nullable=False, default=0)
    last_dispatch_error = Column(Text)
    # Slice 3b (Phase 12.1.5): scheduled-dispatch support. NULL =
    # dispatch immediately (existing behavior). Non-NULL = dispatch
    # not before this time (used for retry-with-backoff on message
    # delivery, where the retry instant is computed at outcome time).
    scheduled_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "task_type IN ('deliver', 'retry', 'deliver_message', 'retry_message')",
            name="valid_task_type",
        ),
        CheckConstraint(
            # Cue-task rows must have execution_id + cue_id populated.
            # Message-task rows must have neither (message_id lives in payload).
            "(task_type IN ('deliver', 'retry') AND execution_id IS NOT NULL AND cue_id IS NOT NULL) "
            "OR (task_type IN ('deliver_message', 'retry_message') AND execution_id IS NULL AND cue_id IS NULL)",
            name="task_payload_shape",
        ),
    )
