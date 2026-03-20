from __future__ import annotations

import uuid

from sqlalchemy import Column, DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class DeviceCode(Base):
    __tablename__ = "device_codes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_code = Column(String(128), unique=True, nullable=False, index=True)
    email = Column(String(255))
    status = Column(String(20), nullable=False, default="pending")
    api_key_plaintext = Column(String(64))
    verification_token = Column(String(64), index=True)
    session_token = Column(String(64), index=True)
    user_id = Column(UUID(as_uuid=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
