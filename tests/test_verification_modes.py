"""Verification-mode behavior on outcome report.

Ten tests: 5 modes × (satisfied / unsatisfied / inapplicable) shapes.
Each test creates a cue with a specific verification mode, claims a
pending execution, reports an outcome, and asserts the resulting
``outcome_state``.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cue import Cue
from app.models.execution import Execution
from app.models.user import User


def _cue_id() -> str:
    return f"cue_{uuid.uuid4().hex[:12]}"


async def _get_user_id(session: AsyncSession, user: dict) -> str:
    result = await session.execute(select(User.id).where(User.email == user["email"]))
    return str(result.scalar_one())


async def _make_cue(session, user_id, *, verification_mode=None, transport="webhook"):
    cue = Cue(
        id=_cue_id(),
        user_id=user_id,
        name=f"t-{uuid.uuid4().hex[:6]}",
        schedule_type="once",
        schedule_at=datetime.now(timezone.utc) + timedelta(hours=1),
        next_run=datetime.now(timezone.utc) + timedelta(hours=1),
        callback_url="https://example.com/hook" if transport == "webhook" else None,
        callback_method="POST",
        callback_transport=transport,
        status="active",
        payload={"task": "t"},
        retry_max_attempts=3,
        retry_backoff_minutes=[1, 5, 15],
        on_failure={"email": False, "webhook": None, "pause": False},
        verification_mode=verification_mode,
    )
    session.add(cue)
    await session.commit()
    return cue


async def _make_execution(session, cue_id):
    ex = Execution(
        id=uuid.uuid4(),
        cue_id=cue_id,
        scheduled_for=datetime.now(timezone.utc),
        status="delivering",
    )
    session.add(ex)
    await session.commit()
    return ex


async def _post_outcome(client: AsyncClient, headers, exec_id, **body):
    body.setdefault("success", True)
    return await client.post(
        f"/v1/executions/{exec_id}/outcome", headers=headers, json=body
    )


class TestModeNone:
    @pytest.mark.asyncio
    async def test_success_marks_reported_success(
        self, client, auth_headers, db_session, registered_user
    ):
        user_id = await _get_user_id(db_session, registered_user)
        cue = await _make_cue(db_session, user_id, verification_mode=None)
        ex = await _make_execution(db_session, cue.id)

        resp = await _post_outcome(client, auth_headers, ex.id, success=True)
        assert resp.status_code == 200
        assert resp.json()["outcome_state"] == "reported_success"

    @pytest.mark.asyncio
    async def test_failure_marks_reported_failure(
        self, client, auth_headers, db_session, registered_user
    ):
        user_id = await _get_user_id(db_session, registered_user)
        cue = await _make_cue(db_session, user_id, verification_mode="none")
        ex = await _make_execution(db_session, cue.id)

        resp = await _post_outcome(
            client, auth_headers, ex.id, success=False, error="boom"
        )
        assert resp.status_code == 200
        assert resp.json()["outcome_state"] == "reported_failure"


class TestModeRequireExternalId:
    @pytest.mark.asyncio
    async def test_satisfied_marks_verified_success(
        self, client, auth_headers, db_session, registered_user
    ):
        user_id = await _get_user_id(db_session, registered_user)
        cue = await _make_cue(
            db_session, user_id, verification_mode="require_external_id"
        )
        ex = await _make_execution(db_session, cue.id)

        resp = await _post_outcome(
            client, auth_headers, ex.id, success=True, external_id="ext-abc-123"
        )
        assert resp.status_code == 200
        assert resp.json()["outcome_state"] == "verified_success"

    @pytest.mark.asyncio
    async def test_missing_marks_verification_failed(
        self, client, auth_headers, db_session, registered_user
    ):
        user_id = await _get_user_id(db_session, registered_user)
        cue = await _make_cue(
            db_session, user_id, verification_mode="require_external_id"
        )
        ex = await _make_execution(db_session, cue.id)

        resp = await _post_outcome(client, auth_headers, ex.id, success=True)
        assert resp.status_code == 200
        assert resp.json()["outcome_state"] == "verification_failed"


class TestModeRequireResultUrl:
    @pytest.mark.asyncio
    async def test_satisfied_marks_verified_success(
        self, client, auth_headers, db_session, registered_user
    ):
        user_id = await _get_user_id(db_session, registered_user)
        cue = await _make_cue(
            db_session, user_id, verification_mode="require_result_url"
        )
        ex = await _make_execution(db_session, cue.id)

        resp = await _post_outcome(
            client,
            auth_headers,
            ex.id,
            success=True,
            result_url="https://example.com/receipts/42",
        )
        assert resp.status_code == 200
        assert resp.json()["outcome_state"] == "verified_success"

    @pytest.mark.asyncio
    async def test_missing_marks_verification_failed(
        self, client, auth_headers, db_session, registered_user
    ):
        user_id = await _get_user_id(db_session, registered_user)
        cue = await _make_cue(
            db_session, user_id, verification_mode="require_result_url"
        )
        ex = await _make_execution(db_session, cue.id)

        resp = await _post_outcome(client, auth_headers, ex.id, success=True)
        assert resp.status_code == 200
        assert resp.json()["outcome_state"] == "verification_failed"


class TestModeRequireArtifacts:
    @pytest.mark.asyncio
    async def test_satisfied_marks_verified_success(
        self, client, auth_headers, db_session, registered_user
    ):
        user_id = await _get_user_id(db_session, registered_user)
        cue = await _make_cue(
            db_session, user_id, verification_mode="require_artifacts"
        )
        ex = await _make_execution(db_session, cue.id)

        resp = await _post_outcome(
            client,
            auth_headers,
            ex.id,
            success=True,
            artifacts=[{"type": "file", "url": "https://x.com/a.pdf"}],
        )
        assert resp.status_code == 200
        assert resp.json()["outcome_state"] == "verified_success"

    @pytest.mark.asyncio
    async def test_missing_marks_verification_failed(
        self, client, auth_headers, db_session, registered_user
    ):
        user_id = await _get_user_id(db_session, registered_user)
        cue = await _make_cue(
            db_session, user_id, verification_mode="require_artifacts"
        )
        ex = await _make_execution(db_session, cue.id)

        resp = await _post_outcome(client, auth_headers, ex.id, success=True)
        assert resp.status_code == 200
        assert resp.json()["outcome_state"] == "verification_failed"


class TestModeManual:
    @pytest.mark.asyncio
    async def test_success_parks_in_verification_pending(
        self, client, auth_headers, db_session, registered_user
    ):
        user_id = await _get_user_id(db_session, registered_user)
        cue = await _make_cue(db_session, user_id, verification_mode="manual")
        ex = await _make_execution(db_session, cue.id)

        resp = await _post_outcome(
            client,
            auth_headers,
            ex.id,
            success=True,
            external_id="irrelevant",  # evidence present but ignored under manual
        )
        assert resp.status_code == 200
        assert resp.json()["outcome_state"] == "verification_pending"

    @pytest.mark.asyncio
    async def test_failure_still_reported_failure(
        self, client, auth_headers, db_session, registered_user
    ):
        # Failure bypasses verification — manual mode doesn't park
        # failed outcomes.
        user_id = await _get_user_id(db_session, registered_user)
        cue = await _make_cue(db_session, user_id, verification_mode="manual")
        ex = await _make_execution(db_session, cue.id)

        resp = await _post_outcome(
            client, auth_headers, ex.id, success=False, error="nope"
        )
        assert resp.status_code == 200
        assert resp.json()["outcome_state"] == "reported_failure"
