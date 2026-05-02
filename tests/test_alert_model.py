"""Alert model: CRUD, CHECK constraints, indexes."""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alert import Alert
from app.models.user import User


async def _make_user(session: AsyncSession):
    suffix = uuid.uuid4().hex[:8]
    u = User(
        email=f"a-{suffix}@test.com",
        api_key_hash=uuid.uuid4().hex,
        api_key_prefix="cue_sk_test",
        webhook_secret="x" * 64,
        slug=f"a-{suffix}",
    )
    session.add(u)
    await session.commit()
    return u


class TestAlertCRUD:
    @pytest.mark.asyncio
    async def test_create_and_read(self, db_session):
        u = await _make_user(db_session)
        a = Alert(
            id=uuid.uuid4(),
            user_id=u.id,
            alert_type="verification_failed",
            message="test",
            alert_metadata={"k": "v"},
        )
        db_session.add(a)
        await db_session.commit()

        row = await db_session.execute(select(Alert).where(Alert.id == a.id))
        got = row.scalar_one()
        assert got.alert_type == "verification_failed"
        assert got.severity == "warning"  # server default
        assert got.acknowledged is False
        assert got.alert_metadata == {"k": "v"}


class TestAlertConstraints:
    @pytest.mark.asyncio
    async def test_invalid_alert_type_rejected(self, db_session):
        u = await _make_user(db_session)
        a = Alert(
            id=uuid.uuid4(),
            user_id=u.id,
            alert_type="not_a_real_type",
            message="x",
        )
        db_session.add(a)
        with pytest.raises(IntegrityError):
            await db_session.commit()
        await db_session.rollback()

    @pytest.mark.asyncio
    async def test_invalid_severity_rejected(self, db_session):
        u = await _make_user(db_session)
        a = Alert(
            id=uuid.uuid4(),
            user_id=u.id,
            alert_type="outcome_timeout",
            severity="cosmic",
            message="x",
        )
        db_session.add(a)
        with pytest.raises(IntegrityError):
            await db_session.commit()
        await db_session.rollback()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "t", ["outcome_timeout", "verification_failed", "consecutive_failures"]
    )
    async def test_valid_alert_types_accepted(self, db_session, t):
        u = await _make_user(db_session)
        a = Alert(id=uuid.uuid4(), user_id=u.id, alert_type=t, message="ok")
        db_session.add(a)
        await db_session.commit()


class TestAlertIndexes:
    @pytest.mark.asyncio
    async def test_indexes_exist(self, db_session):
        # Sanity: the three expected indexes are created. Use pg_indexes.
        rows = await db_session.execute(text(
            "SELECT indexname FROM pg_indexes WHERE tablename='alerts'"
        ))
        names = {r[0] for r in rows.all()}
        # index names from model definition
        assert "ix_alerts_user_created" in names
        assert "ix_alerts_execution_id" in names
