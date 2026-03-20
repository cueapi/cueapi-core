"""Outcome edge case tests — metadata size limits, type validation.

Supplements test_outcome.py with boundary condition tests.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.models.cue import Cue
from app.models.execution import Execution
from app.models.user import User
from app.utils.ids import generate_cue_id, hash_api_key


async def _make_execution(db_session, registered_user):
    """Helper to create a cue + execution for testing."""
    api_key_hash = hash_api_key(registered_user["api_key"])
    result = await db_session.execute(select(User).where(User.api_key_hash == api_key_hash))
    user = result.scalar_one()

    cue_id = generate_cue_id()
    cue = Cue(
        id=cue_id, user_id=str(user.id), name=f"outcome-edge-{uuid.uuid4().hex[:6]}",
        status="active", schedule_type="recurring", schedule_cron="0 9 * * *",
        schedule_timezone="UTC", callback_url="https://example.com/hook",
        callback_method="POST", callback_headers={}, payload={},
        retry_max_attempts=3, retry_backoff_minutes=5,
        next_run=datetime.now(timezone.utc),
    )
    db_session.add(cue)
    await db_session.flush()

    exec_id = uuid.uuid4()
    execution = Execution(
        id=exec_id, cue_id=cue_id, scheduled_for=datetime.now(timezone.utc),
        status="success", http_status=200, attempts=1,
    )
    db_session.add(execution)
    await db_session.commit()

    return str(exec_id), {"Authorization": f"Bearer {registered_user['api_key']}"}


@pytest.mark.asyncio
async def test_outcome_metadata_at_10kb_accepted(client, registered_user, db_session):
    """Metadata at exactly 10KB should be accepted."""
    exec_id, headers = await _make_execution(db_session, registered_user)

    # Create metadata that's just under 10KB when JSON-serialized
    # 10240 bytes = 10KB. JSON overhead of {"k":"..."} is ~7 bytes
    metadata = {"k": "x" * 10220}
    # Verify it's under 10KB
    assert len(json.dumps(metadata).encode("utf-8")) <= 10240

    resp = await client.post(
        f"/v1/executions/{exec_id}/outcome",
        json={"success": True, "result": "ok", "metadata": metadata},
        headers=headers,
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_outcome_metadata_over_10kb_rejected(client, registered_user, db_session):
    """Metadata over 10KB should return 400."""
    exec_id, headers = await _make_execution(db_session, registered_user)

    # Create metadata over 10KB
    metadata = {"k": "x" * 11000}
    assert len(json.dumps(metadata).encode("utf-8")) > 10240

    resp = await client.post(
        f"/v1/executions/{exec_id}/outcome",
        json={"success": True, "result": "ok", "metadata": metadata},
        headers=headers,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "metadata_too_large"


@pytest.mark.asyncio
async def test_outcome_empty_metadata_accepted(client, registered_user, db_session):
    """Empty metadata {} should be accepted."""
    exec_id, headers = await _make_execution(db_session, registered_user)

    resp = await client.post(
        f"/v1/executions/{exec_id}/outcome",
        json={"success": True, "result": "done", "metadata": {}},
        headers=headers,
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_outcome_null_metadata_accepted(client, registered_user, db_session):
    """Null metadata should be accepted (optional field)."""
    exec_id, headers = await _make_execution(db_session, registered_user)

    resp = await client.post(
        f"/v1/executions/{exec_id}/outcome",
        json={"success": True, "result": "done", "metadata": None},
        headers=headers,
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_outcome_no_result_or_error(client, registered_user, db_session):
    """Outcome with only success=True and no result/error should be accepted."""
    exec_id, headers = await _make_execution(db_session, registered_user)

    resp = await client.post(
        f"/v1/executions/{exec_id}/outcome",
        json={"success": True},
        headers=headers,
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_outcome_missing_success_field(client, registered_user, db_session):
    """Outcome without success field should return 422."""
    exec_id, headers = await _make_execution(db_session, registered_user)

    resp = await client.post(
        f"/v1/executions/{exec_id}/outcome",
        json={"result": "done"},
        headers=headers,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_outcome_nonexistent_uuid_returns_404(client, auth_headers):
    """Valid UUID format but nonexistent execution should return 404."""
    fake_uuid = "00000000-0000-0000-0000-000000000000"
    resp = await client.post(
        f"/v1/executions/{fake_uuid}/outcome",
        json={"success": True},
        headers=auth_headers,
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "execution_not_found"


@pytest.mark.asyncio
async def test_outcome_requires_auth(client):
    """Outcome endpoint requires authentication."""
    fake_id = str(uuid.uuid4())
    resp = await client.post(
        f"/v1/executions/{fake_id}/outcome",
        json={"success": True},
    )
    assert resp.status_code == 401
