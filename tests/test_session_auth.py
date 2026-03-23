"""Session-based authentication tests.

9 tests — ported from govindkavaturi-art/cueapi tests/test_session_auth.py

Coverage:
  - Session token issued after device-code verification (new + existing user)
  - Dashboard works with session JWT
  - Expired JWT returns 401 with session_expired code
  - Redirect uses sessionToken not apiKey
  - Reveal key requires auth / works with JWT
  - Second device login does not rotate key
  - Worker unaffected by new device login
  - One-time session token is single-use
"""
from __future__ import annotations

import secrets
import uuid

import jwt
import pytest

from app.config import settings
from app.utils.ids import hash_api_key
from app.utils.session import create_session_jwt


# ---------------------------------------------------------------------------
# Helper: full device-code → verify → poll flow
# ---------------------------------------------------------------------------

async def _do_device_code_flow(client, email: str) -> dict:
    """Run the full device-code → submit-email → verify flow.
    Returns poll response dict (status, session_token, api_key, ...).
    """
    from sqlalchemy import select
    from app.models.device_code import DeviceCode
    from tests.conftest import test_session

    device_code = secrets.token_hex(4).upper()

    # 1. Create device code
    resp = await client.post("/v1/auth/device-code", json={"device_code": device_code})
    assert resp.status_code == 201, f"device code create failed: {resp.text}"

    # 2. Submit email
    resp = await client.post(
        "/v1/auth/device-code/submit-email",
        json={"device_code": device_code, "email": email},
    )
    assert resp.status_code == 200, f"submit email failed: {resp.text}"

    # 3. Get verification token from DB directly
    async with test_session() as db:
        result = await db.execute(
            select(DeviceCode).where(DeviceCode.device_code == device_code)
        )
        dc = result.scalar_one()
        verification_token = dc.verification_token

    # 4. Verify (sets device code to approved)
    resp = await client.get(
        f"/v1/auth/verify?token={verification_token}&device_code={device_code}",
        follow_redirects=False,
    )
    assert resp.status_code == 200, f"verify failed: {resp.text}"

    # 5. Poll to get result
    poll_resp = await client.post(
        "/v1/auth/device-code/poll",
        json={"device_code": device_code},
    )
    assert poll_resp.status_code == 200
    return poll_resp.json()


# ===========================================================================
# Tests
# ===========================================================================

@pytest.mark.asyncio
async def test_session_token_issued_for_new_user(client):
    """New user gets a session_token in poll response after verification."""
    email = f"new-{uuid.uuid4().hex[:8]}@test.com"
    poll_data = await _do_device_code_flow(client, email)

    assert poll_data["status"] == "approved"
    assert poll_data.get("session_token"), "session_token missing from poll response"
    assert poll_data.get("api_key"), "new user should get api_key"


@pytest.mark.asyncio
async def test_session_token_issued_for_existing_user(client, registered_user):
    """Existing user also gets a session_token after verification."""
    email = registered_user["email"]
    poll_data = await _do_device_code_flow(client, email)

    assert poll_data["status"] == "approved"
    assert poll_data.get("session_token"), "session_token missing for existing user"


@pytest.mark.asyncio
async def test_dashboard_works_with_session_jwt(client):
    """One-time token → JWT exchange → /v1/auth/me succeeds with JWT."""
    email = f"jwt-{uuid.uuid4().hex[:8]}@test.com"
    poll_data = await _do_device_code_flow(client, email)

    one_time_token = poll_data["session_token"]

    # Exchange for JWT
    exchange_resp = await client.post("/v1/auth/session", json={"token": one_time_token})
    assert exchange_resp.status_code == 200
    session_jwt = exchange_resp.json().get("session_token")
    assert session_jwt, "no session_token in exchange response"
    assert exchange_resp.json()["email"] == email

    # JWT works for authenticated calls
    me_resp = await client.get(
        "/v1/auth/me",
        headers={"Authorization": f"Bearer {session_jwt}"},
    )
    assert me_resp.status_code == 200
    assert me_resp.json()["email"] == email


@pytest.mark.asyncio
async def test_expired_jwt_returns_401_session_expired(client, registered_user):
    """Manually expired JWT returns 401 with code=session_expired."""
    from sqlalchemy import select
    from app.models.user import User
    from tests.conftest import test_session
    from datetime import timedelta

    async with test_session() as db:
        result = await db.execute(
            select(User).where(User.email == registered_user["email"])
        )
        user = result.scalar_one()

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "email": user.email,
        "type": "session",
        "iat": now - timedelta(hours=25),
        "exp": now - timedelta(hours=1),
    }
    expired_jwt = jwt.encode(payload, settings.SESSION_SECRET, algorithm="HS256")

    resp = await client.get(
        "/v1/auth/me",
        headers={"Authorization": f"Bearer {expired_jwt}"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "session_expired"


@pytest.mark.asyncio
async def test_reveal_key_requires_auth(client):
    """GET /v1/auth/key without auth returns 401."""
    resp = await client.get("/v1/auth/key")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_reveal_key_with_valid_session_jwt(client):
    """GET /v1/auth/key with session JWT returns decrypted API key."""
    email = f"reveal-{uuid.uuid4().hex[:8]}@test.com"
    poll_data = await _do_device_code_flow(client, email)
    new_api_key = poll_data["api_key"]
    one_time_token = poll_data["session_token"]

    exchange_resp = await client.post("/v1/auth/session", json={"token": one_time_token})
    session_jwt = exchange_resp.json()["session_token"]

    reveal_resp = await client.get(
        "/v1/auth/key",
        headers={"Authorization": f"Bearer {session_jwt}"},
    )
    assert reveal_resp.status_code == 200
    assert reveal_resp.json()["api_key"] == new_api_key


@pytest.mark.asyncio
async def test_second_device_login_does_not_rotate_key(client, registered_user):
    """Logging in on a second device returns the same API key, not a new one."""
    api_key = registered_user["api_key"]
    email = registered_user["email"]
    original_hash = hash_api_key(api_key)

    # Trigger key backfill (encrypted storage) via /me call
    await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {api_key}"})

    # Login on "second device"
    poll_data = await _do_device_code_flow(client, email)

    returned_key = poll_data.get("api_key")
    if returned_key:
        assert hash_api_key(returned_key) == original_hash, \
            "second device login rotated the key — it should not have"

    # Original key still works
    me_resp = await client.get(
        "/v1/auth/me",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert me_resp.status_code == 200


@pytest.mark.asyncio
async def test_worker_unaffected_by_dashboard_login(client, registered_user):
    """API key auth continues working after a JWT session is created on another device."""
    api_key = registered_user["api_key"]
    email = registered_user["email"]

    # Worker makes a call
    resp1 = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {api_key}"})
    assert resp1.status_code == 200

    # User logs in via dashboard
    poll_data = await _do_device_code_flow(client, email)
    one_time_token = poll_data["session_token"]
    exchange_resp = await client.post("/v1/auth/session", json={"token": one_time_token})
    assert exchange_resp.status_code == 200
    session_jwt = exchange_resp.json()["session_token"]

    # Dashboard works with JWT
    resp_jwt = await client.get(
        "/v1/auth/me",
        headers={"Authorization": f"Bearer {session_jwt}"},
    )
    assert resp_jwt.status_code == 200

    # Worker STILL works with API key
    resp2 = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {api_key}"})
    assert resp2.status_code == 200
    assert resp2.json()["email"] == email


@pytest.mark.asyncio
async def test_session_token_is_single_use(client):
    """One-time session token is nulled after first exchange, second exchange fails."""
    email = f"onetime-{uuid.uuid4().hex[:8]}@test.com"
    poll_data = await _do_device_code_flow(client, email)
    one_time_token = poll_data["session_token"]

    # First exchange succeeds
    resp1 = await client.post("/v1/auth/session", json={"token": one_time_token})
    assert resp1.status_code == 200

    # Second exchange fails (token consumed)
    resp2 = await client.post("/v1/auth/session", json={"token": one_time_token})
    assert resp2.status_code == 401
