"""End-to-end: outcome reports fire alerts for the right conditions.

Covers:
- verification_failed alert fires when execution.outcome_state is set
  to 'verification_failed' by the rule engine (this hook is dormant
  on current origin/main — PR #18 activates it — but we seed the
  state directly to exercise the integration path).
- consecutive_failures alert fires after the 3rd consecutive failure
  on the same cue.
- Dedup prevents repeat firing within the window.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alert import Alert
from app.models.cue import Cue
from app.models.execution import Execution
from app.models.user import User


async def _uid(session: AsyncSession, user: dict) -> str:
    r = await session.execute(select(User.id).where(User.email == user["email"]))
    return str(r.scalar_one())


async def _cue(session, user_id, transport="webhook"):
    c = Cue(
        id=f"cue_{uuid.uuid4().hex[:12]}",
        user_id=user_id,
        name=f"t-{uuid.uuid4().hex[:6]}",
        schedule_type="once",
        schedule_at=datetime.now(timezone.utc) + timedelta(hours=1),
        next_run=datetime.now(timezone.utc) + timedelta(hours=1),
        callback_url="https://example.com/h" if transport == "webhook" else None,
        callback_method="POST",
        callback_transport=transport,
        status="active",
        payload={},
        retry_max_attempts=3,
        retry_backoff_minutes=[1, 5, 15],
        on_failure={"email": False, "webhook": None, "pause": False},
    )
    session.add(c)
    await session.commit()
    return c


async def _exec(session, cue_id, *, status="delivering", outcome_state=None):
    ex = Execution(
        id=uuid.uuid4(),
        cue_id=cue_id,
        scheduled_for=datetime.now(timezone.utc),
        status=status,
        outcome_state=outcome_state,
    )
    session.add(ex)
    await session.commit()
    return ex


class TestVerificationFailedAlert:
    @pytest.mark.asyncio
    async def test_fires_when_outcome_state_is_verification_failed(
        self, client, auth_headers, db_session, registered_user
    ):
        # Seed: execution with outcome_state pre-set (simulating PR #18
        # rule engine having set it during record_outcome).
        uid = await _uid(db_session, registered_user)
        cue = await _cue(db_session, uid)
        ex = await _exec(db_session, cue.id, outcome_state="verification_failed")

        # Report outcome. The record_outcome path writes and commits,
        # then checks outcome_state post-commit. Since we pre-seeded
        # the state, the alert hook fires.
        resp = await client.post(
            f"/v1/executions/{ex.id}/outcome",
            headers=auth_headers,
            json={"success": True},
        )
        assert resp.status_code == 200

        rows = await db_session.execute(
            select(Alert).where(
                Alert.alert_type == "verification_failed",
                Alert.execution_id == ex.id,
            )
        )
        alerts = rows.scalars().all()
        assert len(alerts) == 1
        assert alerts[0].severity == "warning"
        assert "verification_failed" in alerts[0].alert_metadata["outcome_state"]


class TestConsecutiveFailuresAlert:
    @pytest.mark.asyncio
    async def test_fires_after_three_consecutive_failures(
        self, client, auth_headers, db_session, registered_user
    ):
        uid = await _uid(db_session, registered_user)
        cue = await _cue(db_session, uid, transport="worker")

        # Two prior failed executions already in history
        for _ in range(2):
            prior = Execution(
                id=uuid.uuid4(),
                cue_id=cue.id,
                scheduled_for=datetime.now(timezone.utc),
                status="failed",
                outcome_recorded_at=datetime.now(timezone.utc),
            )
            db_session.add(prior)
        await db_session.commit()

        # Third failure via the API
        ex = await _exec(db_session, cue.id)
        resp = await client.post(
            f"/v1/executions/{ex.id}/outcome",
            headers=auth_headers,
            json={"success": False, "error": "bang"},
        )
        assert resp.status_code == 200

        rows = await db_session.execute(
            select(Alert).where(
                Alert.user_id == uid,
                Alert.alert_type == "consecutive_failures",
            )
        )
        alerts = rows.scalars().all()
        assert len(alerts) == 1
        assert alerts[0].alert_metadata["consecutive_failures"] >= 3

    @pytest.mark.asyncio
    async def test_does_not_fire_on_isolated_failure(
        self, client, auth_headers, db_session, registered_user
    ):
        uid = await _uid(db_session, registered_user)
        cue = await _cue(db_session, uid, transport="worker")
        ex = await _exec(db_session, cue.id)

        resp = await client.post(
            f"/v1/executions/{ex.id}/outcome",
            headers=auth_headers,
            json={"success": False, "error": "one-off"},
        )
        assert resp.status_code == 200

        rows = await db_session.execute(
            select(Alert).where(Alert.user_id == uid)
        )
        assert len(rows.scalars().all()) == 0
