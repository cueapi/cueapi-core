"""GET /v1/alerts: filters, pagination, auth scoping."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alert import Alert
from app.models.user import User


async def _uid(session: AsyncSession, user: dict) -> str:
    r = await session.execute(select(User.id).where(User.email == user["email"]))
    return str(r.scalar_one())


async def _seed(session, user_id, alert_type="verification_failed", n=1, minutes_ago=0):
    for _ in range(n):
        a = Alert(
            id=uuid.uuid4(),
            user_id=user_id,
            alert_type=alert_type,
            message="seeded",
        )
        session.add(a)
    await session.commit()


class TestListAlerts:
    @pytest.mark.asyncio
    async def test_empty(self, client, auth_headers):
        resp = await client.get("/v1/alerts", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["alerts"] == []

    @pytest.mark.asyncio
    async def test_list_own_alerts(self, client, auth_headers, db_session, registered_user):
        uid = await _uid(db_session, registered_user)
        await _seed(db_session, uid, n=3)
        resp = await client.get("/v1/alerts", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert len(data["alerts"]) == 3

    @pytest.mark.asyncio
    async def test_filter_by_type(self, client, auth_headers, db_session, registered_user):
        uid = await _uid(db_session, registered_user)
        await _seed(db_session, uid, alert_type="verification_failed", n=2)
        await _seed(db_session, uid, alert_type="consecutive_failures", n=1)

        resp = await client.get(
            "/v1/alerts?alert_type=verification_failed", headers=auth_headers
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        for a in data["alerts"]:
            assert a["alert_type"] == "verification_failed"

    @pytest.mark.asyncio
    async def test_invalid_type_rejected(self, client, auth_headers):
        resp = await client.get("/v1/alerts?alert_type=bogus", headers=auth_headers)
        assert resp.status_code == 400
        body = resp.json()
        err = body["detail"]["error"] if "detail" in body else body["error"]
        assert err["code"] == "invalid_filter"

    @pytest.mark.asyncio
    async def test_pagination(self, client, auth_headers, db_session, registered_user):
        uid = await _uid(db_session, registered_user)
        await _seed(db_session, uid, n=5)
        resp = await client.get("/v1/alerts?limit=2&offset=0", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert len(data["alerts"]) == 2

        resp2 = await client.get("/v1/alerts?limit=2&offset=4", headers=auth_headers)
        assert resp2.status_code == 200
        assert len(resp2.json()["alerts"]) == 1


class TestAuthScoping:
    @pytest.mark.asyncio
    async def test_user_a_cannot_see_user_b_alerts(
        self, client, auth_headers, other_auth_headers, db_session, registered_user
    ):
        # Seed alerts for user A
        uid_a = await _uid(db_session, registered_user)
        await _seed(db_session, uid_a, n=2)

        # User B fetches their alerts — should see zero
        resp = await client.get("/v1/alerts", headers=other_auth_headers)
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    @pytest.mark.asyncio
    async def test_unauthenticated_rejected(self, client):
        resp = await client.get("/v1/alerts")
        assert resp.status_code == 401
