"""User-facing webhook config endpoints: PATCH /me + secret mgmt."""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.user import User


class TestPatchMeWebhookUrl:
    @pytest.mark.asyncio
    async def test_set_valid_url(self, client, auth_headers, db_session, registered_user):
        resp = await client.patch(
            "/v1/auth/me",
            headers=auth_headers,
            json={"alert_webhook_url": "https://example.com/alerts"},
        )
        assert resp.status_code == 200, resp.text
        row = await db_session.execute(
            select(User.alert_webhook_url).where(User.email == registered_user["email"])
        )
        assert row.scalar_one() == "https://example.com/alerts"

    @pytest.mark.asyncio
    async def test_empty_string_clears(self, client, auth_headers, db_session, registered_user):
        await client.patch(
            "/v1/auth/me",
            headers=auth_headers,
            json={"alert_webhook_url": "https://example.com/alerts"},
        )
        resp = await client.patch(
            "/v1/auth/me",
            headers=auth_headers,
            json={"alert_webhook_url": ""},
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_ssrf_url_rejected(self, client, auth_headers):
        # 169.254.169.254 is cloud metadata — always blocked
        resp = await client.patch(
            "/v1/auth/me",
            headers=auth_headers,
            json={"alert_webhook_url": "http://169.254.169.254/latest/meta-data"},
        )
        assert resp.status_code == 400
        body = resp.json()
        err = body["detail"]["error"] if "detail" in body else body["error"]
        assert err["code"] == "invalid_alert_webhook_url"


class TestGetWebhookSecret:
    @pytest.mark.asyncio
    async def test_lazy_generate_on_first_call(
        self, client, auth_headers, db_session, registered_user
    ):
        row = await db_session.execute(
            select(User.alert_webhook_secret).where(
                User.email == registered_user["email"]
            )
        )
        assert row.scalar_one() is None  # not yet generated

        resp = await client.get("/v1/auth/alert-webhook-secret", headers=auth_headers)
        assert resp.status_code == 200
        secret = resp.json()["alert_webhook_secret"]
        assert secret and len(secret) == 64

        # Second call returns the same value
        resp2 = await client.get("/v1/auth/alert-webhook-secret", headers=auth_headers)
        assert resp2.json()["alert_webhook_secret"] == secret


class TestRegenerateWebhookSecret:
    @pytest.mark.asyncio
    async def test_requires_confirmation(self, client, auth_headers):
        resp = await client.post(
            "/v1/auth/alert-webhook-secret/regenerate", headers=auth_headers
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_rotates(self, client, auth_headers):
        # Establish initial secret
        r1 = await client.get("/v1/auth/alert-webhook-secret", headers=auth_headers)
        old = r1.json()["alert_webhook_secret"]

        headers = {**auth_headers, "X-Confirm-Destructive": "true"}
        r2 = await client.post(
            "/v1/auth/alert-webhook-secret/regenerate", headers=headers
        )
        assert r2.status_code == 200
        new = r2.json()["alert_webhook_secret"]
        assert new != old
        assert r2.json()["previous_secret_revoked"] is True
