from __future__ import annotations

from sqlalchemy import Column, Date, Integer, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class UsageMonthly(Base):
    __tablename__ = "usage_monthly"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    month_start = Column(Date, nullable=False)
    execution_count = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("user_id", "month_start", name="unique_user_month"),
    )
