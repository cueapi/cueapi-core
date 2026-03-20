"""Phase 9 — Execution Outcome Reporting tests.

9 tests:
1. Report success outcome
2. Report failure outcome
3. Report outcome with metadata
4. Immutable — second call returns 409
5. Wrong user gets 404
6. Nonexistent execution gets 404
7. Outcome appears in cue detail (GET /v1/cues/{id})
8. Outcome is null when not reported
9. Usage includes outcome stats
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app.models.cue import Cue
from app.models.execution import Execution
from app.utils.ids import generate_cue_id


@pytest_asyncio.fixture
async def cue_with_execution(client, registered_user, db_session):
    """Create a cue and an execution for testing outcomes."""
    user_data = registered_user
    # Get user_id from DB via api_key_prefix
    from app.models.user import User
    from sqlalchemy import select

    prefix = user_data["api_key"][:10]
    # We need the user_id. Register gives us the key, we can derive it.
    # The conftest creates a user via /v1/auth/register. Let's query by prefix.
    from app.utils.ids import hash_api_key

    api_key_hash = hash_api_key(user_data["api_key"])
    result = await db_session.execute(select(User).where(User.api_key_hash == api_key_hash))
    user = result.scalar_one()

    cue_id = generate_cue_id()
    cue = Cue(
        id=cue_id,
        user_id=str(user.id),
        name="test-outcome-cue",
        status="active",
        schedule_type="recurring",
        schedule_cron="0 9 * * *",
        schedule_timezone="UTC",
        callback_url="https://example.com/hook",
        callback_method="POST",
        callback_headers={},
        payload={"task": "test"},
        retry_max_attempts=3,
        retry_backoff_minutes=5,
        next_run=datetime.now(timezone.utc),
    )
    db_session.add(cue)
    await db_session.flush()

    exec_id = uuid.uuid4()
    execution = Execution(
        id=exec_id,
        cue_id=cue_id,
        scheduled_for=datetime.now(timezone.utc),
        status="success",
        http_status=200,
        attempts=1,
    )
    db_session.add(execution)
    await db_session.commit()

    return {
        "cue_id": cue_id,
        "execution_id": str(exec_id),
        "user_id": str(user.id),
        "auth_headers": {"Authorization": f"Bearer {user_data['api_key']}"},
    }


@pytest.mark.asyncio
async def test_report_success_outcome(client, cue_with_execution):
    """Test reporting a successful outcome."""
    data = cue_with_execution
    response = await client.post(
        f"/v1/executions/{data['execution_id']}/outcome",
        json={"success": True, "result": "Tweet posted successfully"},
        headers=data["auth_headers"],
    )
    assert response.status_code == 200
    body = response.json()
    assert body["execution_id"] == data["execution_id"]
    assert body["outcome_recorded"] is True


@pytest.mark.asyncio
async def test_report_failure_outcome(client, registered_user, db_session):
    """Test reporting a failure outcome."""
    from app.models.user import User
    from app.utils.ids import hash_api_key
    from sqlalchemy import select

    api_key_hash = hash_api_key(registered_user["api_key"])
    result = await db_session.execute(select(User).where(User.api_key_hash == api_key_hash))
    user = result.scalar_one()

    cue_id = generate_cue_id()
    cue = Cue(
        id=cue_id, user_id=str(user.id), name="fail-cue", status="active",
        schedule_type="once", schedule_timezone="UTC",
        callback_url="https://example.com/hook", callback_method="POST",
        callback_headers={}, payload={}, retry_max_attempts=3,
        retry_backoff_minutes=5, next_run=datetime.now(timezone.utc),
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

    response = await client.post(
        f"/v1/executions/{str(exec_id)}/outcome",
        json={"success": False, "error": "Rate limited by Twitter API"},
        headers={"Authorization": f"Bearer {registered_user['api_key']}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["outcome_recorded"] is True


@pytest.mark.asyncio
async def test_report_outcome_with_metadata(client, cue_with_execution):
    """Test reporting outcome with metadata dict."""
    data = cue_with_execution
    metadata = {"tweet_id": "123456", "likes": 0, "impressions": 42}
    response = await client.post(
        f"/v1/executions/{data['execution_id']}/outcome",
        json={"success": True, "result": "Posted", "metadata": metadata},
        headers=data["auth_headers"],
    )
    assert response.status_code == 200
    assert response.json()["outcome_recorded"] is True


@pytest.mark.asyncio
async def test_outcome_immutable_409(client, cue_with_execution):
    """Test that outcome cannot be overwritten (write-once)."""
    data = cue_with_execution

    # First call succeeds
    resp1 = await client.post(
        f"/v1/executions/{data['execution_id']}/outcome",
        json={"success": True, "result": "First"},
        headers=data["auth_headers"],
    )
    assert resp1.status_code == 200

    # Second call gets 409
    resp2 = await client.post(
        f"/v1/executions/{data['execution_id']}/outcome",
        json={"success": False, "error": "Should not overwrite"},
        headers=data["auth_headers"],
    )
    assert resp2.status_code == 409
    assert resp2.json()["error"]["code"] == "outcome_already_recorded"


@pytest.mark.asyncio
async def test_wrong_user_gets_404(client, cue_with_execution, other_auth_headers):
    """Test that a different user cannot report outcome on someone else's execution."""
    data = cue_with_execution
    response = await client.post(
        f"/v1/executions/{data['execution_id']}/outcome",
        json={"success": True, "result": "Hacked"},
        headers=other_auth_headers,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "execution_not_found"


@pytest.mark.asyncio
async def test_nonexistent_execution_404(client, auth_headers):
    """Test that a nonexistent execution_id returns 404."""
    fake_id = str(uuid.uuid4())
    response = await client.post(
        f"/v1/executions/{fake_id}/outcome",
        json={"success": True, "result": "Ghost"},
        headers=auth_headers,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "execution_not_found"


@pytest.mark.asyncio
async def test_outcome_in_cue_detail(client, cue_with_execution):
    """Test that outcome appears in GET /v1/cues/{id} execution list."""
    data = cue_with_execution

    # Report outcome
    await client.post(
        f"/v1/executions/{data['execution_id']}/outcome",
        json={"success": True, "result": "All good", "metadata": {"key": "val"}},
        headers=data["auth_headers"],
    )

    # Get cue detail
    response = await client.get(
        f"/v1/cues/{data['cue_id']}",
        headers=data["auth_headers"],
    )
    assert response.status_code == 200
    cue = response.json()
    execs = cue["executions"]
    assert len(execs) >= 1

    ex = execs[0]
    assert ex["outcome"] is not None
    assert ex["outcome"]["success"] is True
    assert ex["outcome"]["result"] == "All good"
    assert ex["outcome"]["metadata"] == {"key": "val"}
    assert ex["outcome"]["recorded_at"] is not None


@pytest.mark.asyncio
async def test_outcome_null_when_not_reported(client, registered_user, db_session):
    """Test that outcome is null in execution response when not reported."""
    from app.models.user import User
    from app.utils.ids import hash_api_key
    from sqlalchemy import select

    api_key_hash = hash_api_key(registered_user["api_key"])
    result = await db_session.execute(select(User).where(User.api_key_hash == api_key_hash))
    user = result.scalar_one()

    cue_id = generate_cue_id()
    cue = Cue(
        id=cue_id, user_id=str(user.id), name="no-outcome-cue", status="active",
        schedule_type="recurring", schedule_cron="0 9 * * *", schedule_timezone="UTC",
        callback_url="https://example.com/hook", callback_method="POST",
        callback_headers={}, payload={}, retry_max_attempts=3,
        retry_backoff_minutes=5, next_run=datetime.now(timezone.utc),
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

    response = await client.get(
        f"/v1/cues/{cue_id}",
        headers={"Authorization": f"Bearer {registered_user['api_key']}"},
    )
    assert response.status_code == 200
    execs = response.json()["executions"]
    assert len(execs) == 1
    assert execs[0]["outcome"] is None


@pytest.mark.asyncio
async def test_usage_includes_outcome_stats(client, cue_with_execution):
    """Test that GET /v1/usage includes outcome summary."""
    data = cue_with_execution

    # Report a success outcome
    await client.post(
        f"/v1/executions/{data['execution_id']}/outcome",
        json={"success": True, "result": "Done"},
        headers=data["auth_headers"],
    )

    # Check usage
    response = await client.get("/v1/usage", headers=data["auth_headers"])
    assert response.status_code == 200
    body = response.json()
    assert "outcomes" in body
    assert body["outcomes"]["reported"] >= 1
    assert body["outcomes"]["succeeded"] >= 1
    assert "failed" in body["outcomes"]
