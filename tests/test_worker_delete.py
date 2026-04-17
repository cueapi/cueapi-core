"""Tests for DELETE /v1/workers/{worker_id}."""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.models.worker import Worker


async def _get_user_id(session: AsyncSession, user: dict) -> str:
    result = await session.execute(select(User.id).where(User.email == user["email"]))
    return str(result.scalar_one())


async def _register_worker(session, user_id, worker_id="test-worker-1"):
    from datetime import datetime, timezone
    w = Worker(
        user_id=user_id,
        worker_id=worker_id,
        last_heartbeat=datetime.now(timezone.utc),
    )
    session.add(w)
    await session.commit()
    return w


@pytest.mark.asyncio
async def test_delete_own_worker(client, auth_headers, db_session, registered_user):
    """Delete own worker returns 204."""
    user_id = await _get_user_id(db_session, registered_user)
    worker_id = f"del-test-{uuid.uuid4().hex[:6]}"
    await _register_worker(db_session, user_id, worker_id)

    resp = await client.delete(f"/v1/workers/{worker_id}", headers=auth_headers)
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_nonexistent_worker(client, auth_headers):
    """Delete non-existent worker returns 404."""
    resp = await client.delete("/v1/workers/nonexistent-worker-xyz", headers=auth_headers)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "worker_not_found"


@pytest.mark.asyncio
async def test_delete_other_users_worker(client, auth_headers, db_session):
    """Delete another user's worker returns 404 (no existence leak)."""
    # Create a worker under a different user
    other_user_id = str(uuid.uuid4())
    worker_id = f"other-{uuid.uuid4().hex[:6]}"
    from datetime import datetime, timezone
    w = Worker(
        user_id=other_user_id,
        worker_id=worker_id,
        last_heartbeat=datetime.now(timezone.utc),
    )
    db_session.add(w)
    await db_session.commit()

    # Try to delete as the authenticated user
    resp = await client.delete(f"/v1/workers/{worker_id}", headers=auth_headers)
    assert resp.status_code == 404
