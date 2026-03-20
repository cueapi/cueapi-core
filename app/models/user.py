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
    rate_limit_per_minute = Column(Integer, nullable=False, default=60)
    webhook_secret = Column(String(80), nullable=False)
    api_key_encrypted = Column(String(256), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
