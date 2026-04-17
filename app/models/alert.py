"""Alert model — persisted, user-scoped events fired by outcome
service / poller and optionally delivered to the user's configured
alert webhook."""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.database import Base


class Alert(Base):
    __tablename__ = "alerts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    # Both are nullable — an alert can be about a user-level event
    # (e.g. API key rotation) with no execution or cue attached.
    cue_id = Column(String(20), nullable=True)
    execution_id = Column(UUID(as_uuid=True), nullable=True)
    alert_type = Column(String(50), nullable=False)
    # Kept at warning by default — the UI / webhook receiver decides
    # how to render. ``critical`` is reserved for future use.
    severity = Column(String(20), nullable=False, server_default="warning")
    message = Column(Text, nullable=False)
    # DB column is named ``metadata`` (SQLAlchemy reserves the
    # ``metadata`` attr on Base), Python attr is ``alert_metadata``.
    alert_metadata = Column("metadata", JSONB, nullable=True)
    acknowledged = Column(Boolean, nullable=False, server_default="false")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "alert_type IN ("
            "'outcome_timeout', 'verification_failed', 'consecutive_failures')",
            name="valid_alert_type",
        ),
        CheckConstraint(
            "severity IN ('info', 'warning', 'critical')",
            name="valid_alert_severity",
        ),
        Index("ix_alerts_user_created", "user_id", "created_at"),
        Index("ix_alerts_execution_id", "execution_id"),
    )
