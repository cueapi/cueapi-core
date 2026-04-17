"""POST /verify + POST /verification-pending endpoint behavior.

Explicitly pins the behavior-change in this PR: POST /verify now
accepts {valid: bool, reason: str?}. valid=true transitions to
verified_success (legacy default). valid=false transitions to
verification_failed and records reason on evidence_summary.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cue import Cue
from app.models.execution import Execution
from app.models.user import User


async def _uid(session: AsyncSession, user: dict) -> str:
    r = await session.execute(select(User.id).where(User.email == user["email"]))
    return str(r.scalar_one())


async def _cue(session, user_id, verification_mode=None):
    c = Cue(
        id=f"cue_{uuid.uuid4().hex[:12]}",
        user_id=user_id,
        name=f"v-{uuid.uuid4().hex[:6]}",
        schedule_type="once",
        schedule_at=datetime.now(timezone.utc) + timedelta(hours=1),
        next_run=datetime.now(timezone.utc) + timedelta(hours=1),
        callback_url="https://example.com/h",
        callback_method="POST",
        callback_transport="webhook",
        status="active",
        payload={},
        retry_max_attempts=3,
        retry_backoff_minutes=[1, 5, 15],
        on_failure={"email": False, "webhook": None, "pause": False},
        verification_mode=verification_mode,
    )
    session.add(c)
    await session.commit()
    return c


async def _exec_reported(session, cue_id, *, success=True, state="reported_success"):
    ex = Execution(
        id=uuid.uuid4(),
        cue_id=cue_id,
        scheduled_for=datetime.now(timezone.utc),
        status="success" if success else "failed",
        outcome_recorded_at=datetime.now(timezone.utc),
        outcome_success=success,
        outcome_state=state,
    )
    session.add(ex)
    await session.commit()
    return ex


class TestVerifyValid:
    @pytest.mark.asyncio
    async def test_valid_true_transitions_to_verified_success(
        self, client, auth_headers, db_session, registered_user
    ):
        uid = await _uid(db_session, registered_user)
        cue = await _cue(db_session, uid)
        ex = await _exec_reported(db_session, cue.id)

        resp = await client.post(
            f"/v1/executions/{ex.id}/verify",
            headers=auth_headers,
            json={"valid": True},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["outcome_state"] == "verified_success"
        assert data["valid"] is True

    @pytest.mark.asyncio
    async def test_empty_body_defaults_to_valid_true(
        self, client, auth_headers, db_session, registered_user
    ):
        uid = await _uid(db_session, registered_user)
        cue = await _cue(db_session, uid)
        ex = await _exec_reported(db_session, cue.id)

        resp = await client.post(
            f"/v1/executions/{ex.id}/verify", headers=auth_headers
        )
        assert resp.status_code == 200
        assert resp.json()["outcome_state"] == "verified_success"

    @pytest.mark.asyncio
    async def test_from_reported_failure_accepted(
        self, client, auth_headers, db_session, registered_user
    ):
        # Newly accepted starting state — pre-PR, this was rejected.
        uid = await _uid(db_session, registered_user)
        cue = await _cue(db_session, uid)
        ex = await _exec_reported(
            db_session, cue.id, success=False, state="reported_failure"
        )

        resp = await client.post(
            f"/v1/executions/{ex.id}/verify",
            headers=auth_headers,
            json={"valid": True},
        )
        assert resp.status_code == 200
        assert resp.json()["outcome_state"] == "verified_success"


class TestVerifyInvalid:
    @pytest.mark.asyncio
    async def test_valid_false_transitions_to_verification_failed(
        self, client, auth_headers, db_session, registered_user
    ):
        uid = await _uid(db_session, registered_user)
        cue = await _cue(db_session, uid)
        ex = await _exec_reported(db_session, cue.id)

        resp = await client.post(
            f"/v1/executions/{ex.id}/verify",
            headers=auth_headers,
            json={"valid": False, "reason": "evidence fabricated"},
        )
        assert resp.status_code == 200
        assert resp.json()["outcome_state"] == "verification_failed"
        assert resp.json()["valid"] is False

        await db_session.refresh(ex)
        assert ex.evidence_validation_state == "invalid"
        assert ex.evidence_summary is not None
        assert "evidence fabricated" in ex.evidence_summary

    @pytest.mark.asyncio
    async def test_reason_preserves_existing_summary(
        self, client, auth_headers, db_session, registered_user
    ):
        uid = await _uid(db_session, registered_user)
        cue = await _cue(db_session, uid)
        ex = await _exec_reported(db_session, cue.id)
        ex.evidence_summary = "handler finished"
        await db_session.commit()

        resp = await client.post(
            f"/v1/executions/{ex.id}/verify",
            headers=auth_headers,
            json={"valid": False, "reason": "audit found discrepancy"},
        )
        assert resp.status_code == 200
        await db_session.refresh(ex)
        assert ex.evidence_summary is not None
        assert "handler finished" in ex.evidence_summary
        assert "audit found discrepancy" in ex.evidence_summary


class TestVerifyInvalidState:
    @pytest.mark.asyncio
    async def test_unrecorded_outcome_rejected(
        self, client, auth_headers, db_session, registered_user
    ):
        uid = await _uid(db_session, registered_user)
        cue = await _cue(db_session, uid)
        # Execution with no outcome_state
        ex = Execution(
            id=uuid.uuid4(),
            cue_id=cue.id,
            scheduled_for=datetime.now(timezone.utc),
            status="pending",
        )
        db_session.add(ex)
        await db_session.commit()

        resp = await client.post(
            f"/v1/executions/{ex.id}/verify",
            headers=auth_headers,
            json={"valid": True},
        )
        assert resp.status_code == 409
        body = resp.json()
        err = body["detail"]["error"] if "detail" in body else body["error"]
        assert err["code"] == "invalid_state"


class TestVerificationPending:
    @pytest.mark.asyncio
    async def test_from_reported_success(
        self, client, auth_headers, db_session, registered_user
    ):
        uid = await _uid(db_session, registered_user)
        cue = await _cue(db_session, uid)
        ex = await _exec_reported(db_session, cue.id)

        resp = await client.post(
            f"/v1/executions/{ex.id}/verification-pending",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["outcome_state"] == "verification_pending"

    @pytest.mark.asyncio
    async def test_rejects_when_no_outcome(
        self, client, auth_headers, db_session, registered_user
    ):
        uid = await _uid(db_session, registered_user)
        cue = await _cue(db_session, uid)
        ex = Execution(
            id=uuid.uuid4(),
            cue_id=cue.id,
            scheduled_for=datetime.now(timezone.utc),
            status="pending",
        )
        db_session.add(ex)
        await db_session.commit()

        resp = await client.post(
            f"/v1/executions/{ex.id}/verification-pending",
            headers=auth_headers,
        )
        assert resp.status_code == 409
