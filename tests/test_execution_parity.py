"""Tests for the 10 ported endpoints (execution parity with hosted service)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cue import Cue
from app.models.execution import Execution
from app.models.user import User


def _cue_id() -> str:
    return f"cue_{uuid.uuid4().hex[:12]}"


async def _get_user_id(session: AsyncSession, user: dict) -> str:
    result = await session.execute(select(User.id).where(User.email == user["email"]))
    return str(result.scalar_one())


async def _create_webhook_cue(session, user_id, name=None):
    cue_id = _cue_id()
    now = datetime.now(timezone.utc)
    cue = Cue(
        id=cue_id, user_id=user_id, name=name or f"test-{uuid.uuid4().hex[:6]}",
        schedule_type="once", schedule_at=now + timedelta(hours=1), next_run=now + timedelta(hours=1),
        callback_url="https://example.com/hook", callback_method="POST", callback_transport="webhook",
        status="active", payload={"test": True}, retry_max_attempts=3, retry_backoff_minutes=[1, 5, 15],
        on_failure={"email": False, "webhook": None, "pause": False},
    )
    session.add(cue)
    await session.commit()
    return cue


async def _create_worker_cue(session, user_id, name=None):
    cue_id = _cue_id()
    now = datetime.now(timezone.utc)
    cue = Cue(
        id=cue_id, user_id=user_id, name=name or f"worker-{uuid.uuid4().hex[:6]}",
        schedule_type="once", schedule_at=now + timedelta(hours=1), next_run=now + timedelta(hours=1),
        callback_transport="worker", callback_method="POST", status="active",
        payload={"task": "test"}, retry_max_attempts=3, retry_backoff_minutes=[1, 5, 15],
        on_failure={"email": False, "webhook": None, "pause": False},
    )
    session.add(cue)
    await session.commit()
    return cue


async def _create_execution(session, cue_id, status="pending", **kwargs):
    ex = Execution(
        id=uuid.uuid4(), cue_id=cue_id, scheduled_for=datetime.now(timezone.utc),
        status=status, **kwargs,
    )
    session.add(ex)
    await session.commit()
    return ex


# ── 1. GET /v1/executions ──


class TestListExecutions:
    @pytest.mark.asyncio
    async def test_list_empty(self, client: AsyncClient, auth_headers):
        resp = await client.get("/v1/executions", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "executions" in data
        assert "total" in data
        assert isinstance(data["executions"], list)

    @pytest.mark.asyncio
    async def test_list_with_execution(self, client, auth_headers, db_session, registered_user):
        user_id = await _get_user_id(db_session, registered_user)
        cue = await _create_webhook_cue(db_session, user_id)
        await _create_execution(db_session, cue.id)

        resp = await client.get("/v1/executions", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1

    @pytest.mark.asyncio
    async def test_list_filter_by_status(self, client, auth_headers, db_session, registered_user):
        user_id = await _get_user_id(db_session, registered_user)
        cue = await _create_webhook_cue(db_session, user_id)
        await _create_execution(db_session, cue.id, status="failed")

        resp = await client.get("/v1/executions?status=failed", headers=auth_headers)
        assert resp.status_code == 200
        for ex in resp.json()["executions"]:
            assert ex["status"] == "failed"

    @pytest.mark.asyncio
    async def test_list_requires_auth(self, client):
        resp = await client.get("/v1/executions")
        assert resp.status_code == 401


# ── 2. GET /v1/executions/{id} ──


class TestGetExecution:
    @pytest.mark.asyncio
    async def test_get_existing(self, client, auth_headers, db_session, registered_user):
        user_id = await _get_user_id(db_session, registered_user)
        cue = await _create_webhook_cue(db_session, user_id)
        ex = await _create_execution(db_session, cue.id)

        resp = await client.get(f"/v1/executions/{ex.id}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["id"] == str(ex.id)

    @pytest.mark.asyncio
    async def test_get_not_found(self, client, auth_headers):
        resp = await client.get(f"/v1/executions/{uuid.uuid4()}", headers=auth_headers)
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_requires_auth(self, client):
        resp = await client.get(f"/v1/executions/{uuid.uuid4()}")
        assert resp.status_code == 401


# ── 3. POST /v1/executions/{id}/heartbeat ──


class TestHeartbeat:
    @pytest.mark.asyncio
    async def test_heartbeat_success(self, client, auth_headers, db_session, registered_user):
        user_id = await _get_user_id(db_session, registered_user)
        cue = await _create_worker_cue(db_session, user_id)
        ex = await _create_execution(db_session, cue.id, status="delivering", claimed_by_worker="w1")

        resp = await client.post(f"/v1/executions/{ex.id}/heartbeat", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["acknowledged"] is True

    @pytest.mark.asyncio
    async def test_heartbeat_not_delivering(self, client, auth_headers, db_session, registered_user):
        user_id = await _get_user_id(db_session, registered_user)
        cue = await _create_worker_cue(db_session, user_id)
        ex = await _create_execution(db_session, cue.id, status="pending")

        resp = await client.post(f"/v1/executions/{ex.id}/heartbeat", headers=auth_headers)
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_heartbeat_not_found(self, client, auth_headers):
        resp = await client.post(f"/v1/executions/{uuid.uuid4()}/heartbeat", headers=auth_headers)
        assert resp.status_code == 404


# ── 4. POST /v1/executions/{id}/replay ──


class TestReplay:
    @pytest.mark.asyncio
    async def test_replay_failed(self, client, auth_headers, db_session, registered_user):
        user_id = await _get_user_id(db_session, registered_user)
        cue = await _create_webhook_cue(db_session, user_id)
        ex = await _create_execution(db_session, cue.id, status="failed")

        resp = await client.post(f"/v1/executions/{ex.id}/replay", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["triggered_by"] == "replay"
        assert data["replayed_from"] == str(ex.id)

    @pytest.mark.asyncio
    async def test_replay_in_flight_rejected(self, client, auth_headers, db_session, registered_user):
        user_id = await _get_user_id(db_session, registered_user)
        cue = await _create_webhook_cue(db_session, user_id)
        ex = await _create_execution(db_session, cue.id, status="delivering")

        resp = await client.post(f"/v1/executions/{ex.id}/replay", headers=auth_headers)
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_replay_not_found(self, client, auth_headers):
        resp = await client.post(f"/v1/executions/{uuid.uuid4()}/replay", headers=auth_headers)
        assert resp.status_code == 404


# ── 5. POST /v1/executions/{id}/verify ──


class TestVerify:
    @pytest.mark.asyncio
    async def test_verify_success(self, client, auth_headers, db_session, registered_user):
        user_id = await _get_user_id(db_session, registered_user)
        cue = await _create_webhook_cue(db_session, user_id)
        ex = await _create_execution(db_session, cue.id, status="success",
                                      outcome_success=True, outcome_state="reported_success",
                                      outcome_recorded_at=datetime.now(timezone.utc))

        resp = await client.post(f"/v1/executions/{ex.id}/verify", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["outcome_state"] == "verified_success"

    @pytest.mark.asyncio
    async def test_verify_wrong_state(self, client, auth_headers, db_session, registered_user):
        user_id = await _get_user_id(db_session, registered_user)
        cue = await _create_webhook_cue(db_session, user_id)
        ex = await _create_execution(db_session, cue.id, status="success",
                                      outcome_state="reported_failure",
                                      outcome_recorded_at=datetime.now(timezone.utc))

        resp = await client.post(f"/v1/executions/{ex.id}/verify", headers=auth_headers)
        assert resp.status_code == 409


# ── 6. POST /v1/executions/{id}/verification-pending ──


class TestVerificationPending:
    @pytest.mark.asyncio
    async def test_mark_pending(self, client, auth_headers, db_session, registered_user):
        user_id = await _get_user_id(db_session, registered_user)
        cue = await _create_webhook_cue(db_session, user_id)
        ex = await _create_execution(db_session, cue.id, status="success",
                                      outcome_success=True, outcome_state="reported_success",
                                      outcome_recorded_at=datetime.now(timezone.utc))

        resp = await client.post(f"/v1/executions/{ex.id}/verification-pending", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["outcome_state"] == "verification_pending"

    @pytest.mark.asyncio
    async def test_mark_pending_no_outcome(self, client, auth_headers, db_session, registered_user):
        user_id = await _get_user_id(db_session, registered_user)
        cue = await _create_webhook_cue(db_session, user_id)
        ex = await _create_execution(db_session, cue.id, status="pending")

        resp = await client.post(f"/v1/executions/{ex.id}/verification-pending", headers=auth_headers)
        assert resp.status_code == 409


# ── 7. PATCH /v1/executions/{id}/evidence ──


class TestEvidence:
    @pytest.mark.asyncio
    async def test_append_evidence(self, client, auth_headers, db_session, registered_user):
        user_id = await _get_user_id(db_session, registered_user)
        cue = await _create_webhook_cue(db_session, user_id)
        ex = await _create_execution(db_session, cue.id, status="success",
                                      outcome_success=True, outcome_state="reported_success",
                                      outcome_recorded_at=datetime.now(timezone.utc))

        resp = await client.patch(f"/v1/executions/{ex.id}/evidence", headers=auth_headers,
                                   json={"external_id": "ext-123", "summary": "All good"})
        assert resp.status_code == 200
        assert resp.json()["evidence_updated"] is True

    @pytest.mark.asyncio
    async def test_evidence_no_outcome(self, client, auth_headers, db_session, registered_user):
        user_id = await _get_user_id(db_session, registered_user)
        cue = await _create_webhook_cue(db_session, user_id)
        ex = await _create_execution(db_session, cue.id, status="pending")

        resp = await client.patch(f"/v1/executions/{ex.id}/evidence", headers=auth_headers,
                                   json={"external_id": "ext-123"})
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_evidence_not_found(self, client, auth_headers):
        resp = await client.patch(f"/v1/executions/{uuid.uuid4()}/evidence", headers=auth_headers,
                                   json={"external_id": "ext-123"})
        assert resp.status_code == 404


# ── 8. POST /v1/cues/{id}/fire ──


class TestFireCue:
    @pytest.mark.asyncio
    async def test_fire_creates_execution(self, client, auth_headers, db_session, registered_user):
        user_id = await _get_user_id(db_session, registered_user)
        cue = await _create_webhook_cue(db_session, user_id)

        resp = await client.post(f"/v1/cues/{cue.id}/fire", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["cue_id"] == cue.id
        assert data["status"] == "pending"
        assert data["triggered_by"] == "manual_fire"

    @pytest.mark.asyncio
    async def test_fire_not_found(self, client, auth_headers):
        resp = await client.post("/v1/cues/cue_nonexistent/fire", headers=auth_headers)
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_fire_requires_auth(self, client, db_session, registered_user):
        user_id = await _get_user_id(db_session, registered_user)
        cue = await _create_webhook_cue(db_session, user_id)
        resp = await client.post(f"/v1/cues/{cue.id}/fire")
        assert resp.status_code == 401


# ── 9. POST /v1/auth/session/refresh ──


class TestSessionRefresh:
    @pytest.mark.asyncio
    async def test_refresh_requires_auth(self, client):
        resp = await client.post("/v1/auth/session/refresh")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_refresh_with_api_key(self, client, auth_headers):
        resp = await client.post("/v1/auth/session/refresh", headers=auth_headers)
        # Should return session_token (or 503 if SESSION_SECRET not set in test env)
        assert resp.status_code in (200, 503)
        if resp.status_code == 200:
            assert "session_token" in resp.json()


# ── 10. PATCH /v1/auth/me ──


class TestPatchMe:
    @pytest.mark.asyncio
    async def test_patch_requires_auth(self, client):
        resp = await client.patch("/v1/auth/me", json={"email": "new@test.com"})
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_patch_email(self, client, auth_headers):
        resp = await client.patch("/v1/auth/me", headers=auth_headers, json={"email": f"updated-{uuid.uuid4().hex[:6]}@test.com"})
        assert resp.status_code == 200
        assert "updated_at" in resp.json()

    @pytest.mark.asyncio
    async def test_patch_no_fields(self, client, auth_headers):
        resp = await client.patch("/v1/auth/me", headers=auth_headers, json={})
        assert resp.status_code == 422
