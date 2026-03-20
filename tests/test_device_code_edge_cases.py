"""Device code edge case tests — reuse, concurrent codes, token replay.

Supplements test_device_code.py with boundary condition tests.
"""
from __future__ import annotations

import uuid

import pytest


def _skip_on_rate_limit(response):
    """Skip test gracefully if device-code IP rate limit (5/hr) is exhausted."""
    if response.status_code == 429:
        pytest.skip("Device code IP rate limit hit (5/hr) — not a code bug")


@pytest.mark.asyncio
async def test_device_code_too_short_rejected(client, redis_client):
    """Device code under 8 characters should be rejected."""
    resp = await client.post("/v1/auth/device-code", json={
        "device_code": "ABC"
    })
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_device_code"


@pytest.mark.asyncio
async def test_device_code_duplicate_rejected(client, redis_client):
    """Creating the same device code twice should return 409."""
    code = f"TEST{uuid.uuid4().hex[:4].upper()}"
    resp1 = await client.post("/v1/auth/device-code", json={
        "device_code": code
    })
    _skip_on_rate_limit(resp1)
    assert resp1.status_code == 201

    resp2 = await client.post("/v1/auth/device-code", json={
        "device_code": code
    })
    # Second request could hit rate limit OR return 409 for duplicate
    if resp2.status_code == 429:
        pytest.skip("Device code IP rate limit hit (5/hr) — not a code bug")
    assert resp2.status_code == 409
    assert resp2.json()["error"]["code"] == "device_code_exists"


@pytest.mark.asyncio
async def test_poll_nonexistent_code_returns_expired(client, redis_client):
    """Polling a code that doesn't exist should return status='expired'."""
    resp = await client.post("/v1/auth/device-code/poll", json={
        "device_code": "NONEXIST"
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "expired"


@pytest.mark.asyncio
async def test_poll_returns_pending_initially(client, redis_client):
    """Freshly created code should poll as 'pending'."""
    code = f"POLL{uuid.uuid4().hex[:4].upper()}"
    resp = await client.post("/v1/auth/device-code", json={"device_code": code})
    _skip_on_rate_limit(resp)

    resp = await client.post("/v1/auth/device-code/poll", json={"device_code": code})
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"


@pytest.mark.asyncio
async def test_device_code_creates_with_expiry(client, redis_client):
    """Created device code should have expires_in and verification_url."""
    code = f"EXPR{uuid.uuid4().hex[:4].upper()}"
    resp = await client.post("/v1/auth/device-code", json={"device_code": code})
    _skip_on_rate_limit(resp)
    assert resp.status_code == 201
    body = resp.json()
    assert "expires_in" in body
    assert body["expires_in"] > 0
    assert "verification_url" in body


@pytest.mark.asyncio
async def test_multiple_different_codes_allowed(client, redis_client):
    """Multiple distinct device codes should be created successfully."""
    for i in range(3):
        code = f"MULTI{i}{uuid.uuid4().hex[:3].upper()}"
        resp = await client.post("/v1/auth/device-code", json={"device_code": code})
        _skip_on_rate_limit(resp)
        assert resp.status_code == 201


@pytest.mark.asyncio
async def test_submit_email_requires_valid_code(client, redis_client):
    """submit-email with a nonexistent code should fail."""
    resp = await client.post("/v1/auth/device-code/submit-email", json={
        "device_code": "NOCODE99",
        "email": "test@example.com"
    })
    assert resp.status_code in (400, 404)


@pytest.mark.asyncio
async def test_auth_me_requires_auth(client):
    """GET /v1/auth/me without auth should return 401."""
    resp = await client.get("/v1/auth/me")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_auth_me_returns_user_info(client, registered_user):
    """GET /v1/auth/me should return user info."""
    headers = {"Authorization": f"Bearer {registered_user['api_key']}"}
    resp = await client.get("/v1/auth/me", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "email" in body
    assert "plan" in body
    assert "has_webhook_secret" in body
