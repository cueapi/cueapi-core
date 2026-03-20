from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.models.cue import Cue
from app.models.execution import Execution
from app.models.user import User
from app.utils.ids import generate_cue_id, hash_api_key


@pytest_asyncio.fixture
async def cue_with_executions(client, registered_user, db_session):
    """Create a cue with 25 executions for pagination testing."""
    api_key = registered_user["api_key"]
    headers = {"Authorization": f"Bearer {api_key}"}

    # Look up user_id from API key hash
    key_hash = hash_api_key(api_key)
    result = await db_session.execute(select(User).where(User.api_key_hash == key_hash))
    user = result.scalar_one()
    user_id = str(user.id)

    cue_id = generate_cue_id()
    cue = Cue(
        id=cue_id,
        user_id=user_id,
        name="pagination-test-cue",
        status="active",
        schedule_type="recurring",
        schedule_cron="*/5 * * * *",
        schedule_timezone="UTC",
        callback_url="https://example.com/webhook",
        callback_method="POST",
        callback_transport="webhook",
        payload={},
        retry_max_attempts=3,
        retry_backoff_minutes=[1, 5, 15],
        next_run=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db_session.add(cue)

    # Create 25 executions with distinct timestamps
    base_time = datetime.now(timezone.utc) - timedelta(hours=25)
    for i in range(25):
        scheduled_for = base_time + timedelta(hours=i)
        ex = Execution(
            id=uuid.uuid4(),
            cue_id=cue_id,
            scheduled_for=scheduled_for,
            status="success",
            attempts=1,
            created_at=scheduled_for,
            updated_at=scheduled_for,
        )
        db_session.add(ex)

    await db_session.commit()
    return {"cue_id": cue_id, "headers": headers}


@pytest.mark.asyncio
async def test_default_execution_pagination(client, cue_with_executions, redis_client):
    """Default pagination returns 10 executions with total count."""
    cue_id = cue_with_executions["cue_id"]
    headers = cue_with_executions["headers"]

    resp = await client.get(f"/v1/cues/{cue_id}", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["executions"]) == 10
    assert data["execution_total"] == 25
    assert data["execution_limit"] == 10
    assert data["execution_offset"] == 0


@pytest.mark.asyncio
async def test_execution_pagination_custom_limit(client, cue_with_executions, redis_client):
    """Custom execution_limit returns requested number of executions."""
    cue_id = cue_with_executions["cue_id"]
    headers = cue_with_executions["headers"]

    resp = await client.get(f"/v1/cues/{cue_id}?execution_limit=5", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["executions"]) == 5
    assert data["execution_total"] == 25
    assert data["execution_limit"] == 5
    assert data["execution_offset"] == 0


@pytest.mark.asyncio
async def test_execution_pagination_with_offset(client, cue_with_executions, redis_client):
    """Offset skips executions correctly."""
    cue_id = cue_with_executions["cue_id"]
    headers = cue_with_executions["headers"]

    # Get first page
    resp1 = await client.get(f"/v1/cues/{cue_id}?execution_limit=5&execution_offset=0", headers=headers)
    data1 = resp1.json()

    # Get second page
    resp2 = await client.get(f"/v1/cues/{cue_id}?execution_limit=5&execution_offset=5", headers=headers)
    data2 = resp2.json()

    assert len(data1["executions"]) == 5
    assert len(data2["executions"]) == 5
    assert data1["execution_offset"] == 0
    assert data2["execution_offset"] == 5

    # Pages should have different executions
    ids_page1 = {e["id"] for e in data1["executions"]}
    ids_page2 = {e["id"] for e in data2["executions"]}
    assert ids_page1.isdisjoint(ids_page2)


@pytest.mark.asyncio
async def test_execution_pagination_offset_beyond_total(client, cue_with_executions, redis_client):
    """Offset beyond total returns empty list but correct total."""
    cue_id = cue_with_executions["cue_id"]
    headers = cue_with_executions["headers"]

    resp = await client.get(f"/v1/cues/{cue_id}?execution_offset=100", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["executions"]) == 0
    assert data["execution_total"] == 25
    assert data["execution_offset"] == 100


@pytest.mark.asyncio
async def test_execution_pagination_max_limit(client, cue_with_executions, redis_client):
    """execution_limit max is 100."""
    cue_id = cue_with_executions["cue_id"]
    headers = cue_with_executions["headers"]

    # Valid: limit=100
    resp = await client.get(f"/v1/cues/{cue_id}?execution_limit=100", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["execution_limit"] == 100

    # Invalid: limit=101
    resp = await client.get(f"/v1/cues/{cue_id}?execution_limit=101", headers=headers)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_execution_pagination_invalid_values(client, cue_with_executions, redis_client):
    """Invalid pagination values return 422."""
    cue_id = cue_with_executions["cue_id"]
    headers = cue_with_executions["headers"]

    # Negative offset
    resp = await client.get(f"/v1/cues/{cue_id}?execution_offset=-1", headers=headers)
    assert resp.status_code == 422

    # Zero limit
    resp = await client.get(f"/v1/cues/{cue_id}?execution_limit=0", headers=headers)
    assert resp.status_code == 422
