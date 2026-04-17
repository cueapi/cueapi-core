from sqlalchemy import CheckConstraint, Column, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.database import Base


class Cue(Base):
    __tablename__ = "cues"

    id = Column(String(20), primary_key=True)
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text)
    status = Column(String(20), nullable=False, default="active")
    schedule_type = Column(String(10), nullable=False)
    schedule_cron = Column(String(100))
    schedule_at = Column(DateTime(timezone=True))
    schedule_timezone = Column(String(50), nullable=False, default="UTC")
    callback_url = Column(Text, nullable=True)
    callback_method = Column(String(10), nullable=False, default="POST")
    callback_transport = Column(String(10), nullable=False, default="webhook")
    callback_headers = Column(JSONB, default={})
    payload = Column(JSONB, default={})
    retry_max_attempts = Column(Integer, nullable=False, default=3)
    retry_backoff_minutes = Column(JSONB, nullable=False, default=[1, 5, 15])
    next_run = Column(DateTime(timezone=True), index=True)
    last_run = Column(DateTime(timezone=True))
    run_count = Column(Integer, nullable=False, default=0)
    fired_count = Column(Integer, nullable=False, default=0)
    on_failure = Column(JSONB, nullable=True, default={"email": True, "webhook": None, "pause": False})
    # Outcome-verification policy. NULL == no verification (same as 'none').
    verification_mode = Column(String(50), nullable=True, default=None)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("status IN ('active', 'paused', 'completed', 'failed')", name="valid_status"),
        CheckConstraint("schedule_type IN ('once', 'recurring')", name="valid_schedule_type"),
        CheckConstraint("callback_method IN ('POST', 'GET', 'PUT', 'PATCH')", name="valid_callback_method"),
        CheckConstraint("callback_transport IN ('webhook', 'worker')", name="valid_callback_transport"),
        CheckConstraint(
            "verification_mode IS NULL OR verification_mode IN ("
            "'none', 'require_external_id', 'require_result_url', "
            "'require_artifacts', 'manual')",
            name="valid_verification_mode",
        ),
        UniqueConstraint("user_id", "name", name="unique_user_cue_name"),
    )
