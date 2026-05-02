"""Per-user-per-month message counter.

Mirrors ``usage_monthly`` shape exactly. Quotas SEPARATE from
execution quotas. Service layer dual-writes to Redis (fast) +
Postgres (durable) following the existing ``usage_service`` pattern.
"""
from __future__ import annotations

from sqlalchemy import (
    Column,
    Date,
    ForeignKey,
    Integer,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class UsageMessagesMonthly(Base):
    __tablename__ = "usage_messages_monthly"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    month_start = Column(Date, nullable=False)
    message_count = Column(Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (
        UniqueConstraint("user_id", "month_start", name="unique_user_month_messages"),
    )
