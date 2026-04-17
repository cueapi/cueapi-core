"""OutcomeRequest accepts evidence fields — persistence + backward compat.

Covers:
- Evidence fields on POST /outcome persist to the execution's
  evidence_* columns.
- A request that sends only {success} (the legacy shape) still works
  and leaves evidence_* columns NULL.
- PATCH /v1/executions/{id}/evidence (two-step flow) remains
  functional.
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


async def _user_id(session: AsyncSession, user: dict) -> str:
    r = await session.execute(select(User.id).where(User.email == user["email"]))
    return str(r.scalar_one())


async def _webhook_cue(session, user_id):
    cue = Cue(
        id=f"cue_{uuid.uuid4().hex[:12]}",
        user_id=user_id,
        name=f"ev-{uuid.uuid4().hex[:6]}",
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
    )
    session.add(cue)
    await session.commit()
    return cue


async def _exec(session, cue_id):
    ex = Execution(
        id=uuid.uuid4(),
        cue_id=cue_id,
        scheduled_for=datetime.now(timezone.utc),
        status="delivering",
    )
    session.add(ex)
    await session.commit()
    return ex


class TestInlineEvidenceOnOutcome:
    @pytest.mark.asyncio
    async def test_all_evidence_fields_persist(
        self, client, auth_headers, db_session, registered_user
    ):
        uid = await _user_id(db_session, registered_user)
        cue = await _webhook_cue(db_session, uid)
        ex = await _exec(db_session, cue.id)

        resp = await client.post(
            f"/v1/executions/{ex.id}/outcome",
            headers=auth_headers,
            json={
                "success": True,
                "external_id": "ext-xyz",
                "result_url": "https://example.com/r/1",
                "result_ref": "ref-1",
                "result_type": "document",
                "summary": "Ran successfully",
                "artifacts": [{"type": "log", "url": "https://e.com/l"}],
            },
        )
        assert resp.status_code == 200

        await db_session.refresh(ex)
        assert ex.evidence_external_id == "ext-xyz"
        assert ex.evidence_result_url == "https://example.com/r/1"
        assert ex.evidence_result_ref == "ref-1"
        assert ex.evidence_result_type == "document"
        assert ex.evidence_summary == "Ran successfully"
        assert ex.evidence_artifacts == [
            {"type": "log", "url": "https://e.com/l"}
        ]
        assert ex.evidence_produced_at is not None

    @pytest.mark.asyncio
    async def test_legacy_shape_still_accepted(
        self, client, auth_headers, db_session, registered_user
    ):
        uid = await _user_id(db_session, registered_user)
        cue = await _webhook_cue(db_session, uid)
        ex = await _exec(db_session, cue.id)

        resp = await client.post(
            f"/v1/executions/{ex.id}/outcome",
            headers=auth_headers,
            json={"success": True, "result": "ok"},
        )
        assert resp.status_code == 200

        await db_session.refresh(ex)
        assert ex.evidence_external_id is None
        assert ex.evidence_result_url is None
        assert ex.evidence_artifacts is None

    @pytest.mark.asyncio
    async def test_summary_truncated_to_500(
        self, client, auth_headers, db_session, registered_user
    ):
        # Pydantic caps summary at 500 chars before we even see it —
        # a caller that sends longer fails validation with 422. This
        # pins that behavior.
        uid = await _user_id(db_session, registered_user)
        cue = await _webhook_cue(db_session, uid)
        ex = await _exec(db_session, cue.id)

        resp = await client.post(
            f"/v1/executions/{ex.id}/outcome",
            headers=auth_headers,
            json={"success": True, "summary": "x" * 501},
        )
        assert resp.status_code == 422


class TestPatchEvidenceStillWorks:
    @pytest.mark.asyncio
    async def test_patch_evidence_after_outcome(
        self, client, auth_headers, db_session, registered_user
    ):
        uid = await _user_id(db_session, registered_user)
        cue = await _webhook_cue(db_session, uid)
        ex = await _exec(db_session, cue.id)

        outcome = await client.post(
            f"/v1/executions/{ex.id}/outcome",
            headers=auth_headers,
            json={"success": True},
        )
        assert outcome.status_code == 200

        patch = await client.patch(
            f"/v1/executions/{ex.id}/evidence",
            headers=auth_headers,
            json={"external_id": "after-the-fact"},
        )
        assert patch.status_code == 200
        await db_session.refresh(ex)
        assert ex.evidence_external_id == "after-the-fact"
