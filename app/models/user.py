import uuid

from sqlalchemy import Boolean, Column, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), unique=True, nullable=False, index=True)
    api_key_hash = Column(String(64), nullable=False, unique=True, index=True)
    api_key_prefix = Column(String(12), nullable=False, index=True)
    plan = Column(String(20), nullable=False, default="free")
    active_cue_limit = Column(Integer, nullable=False, default=10)
    monthly_execution_limit = Column(Integer, nullable=False, default=300)
    monthly_message_limit = Column(Integer, nullable=False, default=300, server_default="300")
    rate_limit_per_minute = Column(Integer, nullable=False, default=60)
    # Per-tenant slug used in slug-form addressing (`agent@user_slug`).
    # Backfilled by migration 020 from email local-part; lock-after-set
    # via PATCH /v1/auth/me. Globally unique (the constraint is named
    # `unique_user_slug` to match the migration).
    slug = Column(String(64), nullable=False, unique=True)
    webhook_secret = Column(String(80), nullable=False)
    api_key_encrypted = Column(String(256), nullable=True)
    # Optional HTTPS endpoint that receives alert webhooks (signed).
    # If NULL, alerts are persisted but not delivered — users poll
    # ``GET /v1/alerts``.
    alert_webhook_url = Column(String(2048), nullable=True)
    # HMAC-SHA256 signing key for alert webhook payloads. Generated
    # lazily on first ``GET /v1/auth/alert-webhook-secret`` and rotatable
    # via ``POST /v1/auth/alert-webhook-secret/regenerate``.
    alert_webhook_secret = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
