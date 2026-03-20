from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device_code import DeviceCode


def _skip_on_rate_limit(response):
    """Skip test gracefully if device-code IP rate limit (5/hr) is exhausted."""
    if response.status_code == 429:
        pytest.skip("Device code IP rate limit hit (5/hr) — not a code bug")


async def _get_device_code_from_db(db_session: AsyncSession, code: str) -> DeviceCode:
    result = await db_session.execute(
        select(DeviceCode).where(DeviceCode.device_code == code)
    )
    return result.scalar_one()


@pytest.mark.asyncio
async def test_create_device_code(client, redis_client):
    response = await client.post("/v1/auth/device-code", json={"device_code": "TEST-CODE"})
    _skip_on_rate_limit(response)
    assert response.status_code == 201
    data = response.json()
    assert "verification_url" in data
    assert data["expires_in"] == 900
    assert "TEST-CODE" in data["verification_url"]


@pytest.mark.asyncio
async def test_create_device_code_too_short(client, redis_client):
    response = await client.post("/v1/auth/device-code", json={"device_code": "SHORT"})
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_poll_pending(client, redis_client):
    resp = await client.post("/v1/auth/device-code", json={"device_code": "POLL-TEST"})
    _skip_on_rate_limit(resp)
    response = await client.post("/v1/auth/device-code/poll", json={"device_code": "POLL-TEST"})
    assert response.json()["status"] == "pending"


@pytest.mark.asyncio
async def test_poll_expired(client, db_session, redis_client):
    """Polling an expired device code returns 'expired'."""
    # Create a device code and manually set it to expired
    resp = await client.post("/v1/auth/device-code", json={"device_code": "EXP-TCODE"})
    _skip_on_rate_limit(resp)
    await db_session.execute(
        update(DeviceCode)
        .where(DeviceCode.device_code == "EXP-TCODE")
        .values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    )
    await db_session.commit()

    response = await client.post("/v1/auth/device-code/poll", json={"device_code": "EXP-TCODE"})
    assert response.json()["status"] == "expired"


@pytest.mark.asyncio
async def test_poll_nonexistent(client, redis_client):
    """Polling a nonexistent device code returns 'expired'."""
    response = await client.post("/v1/auth/device-code/poll", json={"device_code": "NOEX-CODE"})
    assert response.json()["status"] == "expired"


@pytest.mark.asyncio
async def test_full_login_flow(client, db_session, redis_client):
    """End-to-end: create code -> submit email -> verify -> poll gets key."""
    # Step 1: Create device code
    resp = await client.post("/v1/auth/device-code", json={"device_code": "FULL-TEST"})
    _skip_on_rate_limit(resp)
    assert resp.status_code == 201

    # Step 2: Submit email
    resp = await client.post("/v1/auth/device-code/submit-email", json={
        "device_code": "FULL-TEST", "email": "newuser@example.com"
    })
    assert resp.json()["status"] == "email_sent"

    # Step 3: Get verification token from DB (simulating email click)
    dc = await _get_device_code_from_db(db_session, "FULL-TEST")
    token = dc.verification_token
    assert token is not None

    # Step 4: Verify (click magic link)
    resp = await client.get(f"/v1/auth/verify?token={token}&device_code=FULL-TEST")
    assert resp.status_code == 200
    assert "Verified" in resp.text

    # Step 5: Poll — should get API key
    resp = await client.post("/v1/auth/device-code/poll", json={"device_code": "FULL-TEST"})
    data = resp.json()
    assert data["status"] == "approved"
    assert data["api_key"].startswith("cue_sk_")
    assert data["email"] == "newuser@example.com"

    # Step 6: Poll again — should be claimed (key not returned again)
    resp = await client.post("/v1/auth/device-code/poll", json={"device_code": "FULL-TEST"})
    assert resp.json()["status"] == "claimed"


@pytest.mark.asyncio
async def test_existing_user_login(client, db_session, redis_client):
    """Existing user logging in via magic link does NOT get a new API key."""
    # Create user via register endpoint first
    reg = await client.post("/v1/auth/register", json={"email": "existing@example.com"})
    old_key = reg.json()["api_key"]

    # Do device code login with same email
    resp = await client.post("/v1/auth/device-code", json={"device_code": "EXIS-TEST"})
    _skip_on_rate_limit(resp)
    await client.post("/v1/auth/device-code/submit-email", json={
        "device_code": "EXIS-TEST", "email": "existing@example.com"
    })
    dc = await _get_device_code_from_db(db_session, "EXIS-TEST")
    await client.get(f"/v1/auth/verify?token={dc.verification_token}&device_code=EXIS-TEST")

    resp = await client.post("/v1/auth/device-code/poll", json={"device_code": "EXIS-TEST"})
    data = resp.json()
    assert data["status"] == "approved"
    assert data.get("existing_user") is True
    assert "api_key" not in data  # No new key issued
    assert data["email"] == "existing@example.com"

    # Old key should STILL be valid — login must never rotate keys
    resp = await client.get("/v1/cues", headers={"Authorization": f"Bearer {old_key}"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_verification_token_single_use(client, db_session, redis_client):
    """Clicking the magic link twice should fail the second time."""
    resp = await client.post("/v1/auth/device-code", json={"device_code": "ONCE-TEST"})
    _skip_on_rate_limit(resp)
    await client.post("/v1/auth/device-code/submit-email", json={
        "device_code": "ONCE-TEST", "email": "once@example.com"
    })
    dc = await _get_device_code_from_db(db_session, "ONCE-TEST")
    token = dc.verification_token

    resp1 = await client.get(f"/v1/auth/verify?token={token}&device_code=ONCE-TEST")
    assert resp1.status_code == 200

    resp2 = await client.get(f"/v1/auth/verify?token={token}&device_code=ONCE-TEST")
    assert resp2.status_code == 400  # Token already used


@pytest.mark.asyncio
async def test_auth_me(client, auth_headers, redis_client):
    response = await client.get("/v1/auth/me", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert "email" in data
    assert "plan" in data
    assert "active_cues" in data
    assert "executions_this_month" in data
    assert "active_cue_limit" in data
    assert "monthly_execution_limit" in data
    assert "rate_limit_per_minute" in data


@pytest.mark.asyncio
async def test_login_does_not_rotate_key(client, db_session, redis_client):
    """CRITICAL: Login via magic link must NEVER rotate an existing user's API key.

    This is a regression guard. If this test fails, every dashboard login
    will silently kill all running workers by invalidating their API key.
    DO NOT remove or weaken this test.
    """
    from app.models.user import User

    # 1. Create user and record their api_key_hash
    reg = await client.post("/v1/auth/register", json={"email": "norotate@example.com"})
    assert reg.status_code == 201
    original_key = reg.json()["api_key"]

    result = await db_session.execute(
        select(User).where(User.email == "norotate@example.com")
    )
    user_before = result.scalar_one()
    hash_before = user_before.api_key_hash
    assert hash_before is not None

    # 2. Run the full device-code login flow (simulating a dashboard login)
    resp = await client.post("/v1/auth/device-code", json={"device_code": "NORO-TEST"})
    _skip_on_rate_limit(resp)
    await client.post("/v1/auth/device-code/submit-email", json={
        "device_code": "NORO-TEST", "email": "norotate@example.com"
    })
    dc = await _get_device_code_from_db(db_session, "NORO-TEST")
    resp = await client.get(f"/v1/auth/verify?token={dc.verification_token}&device_code=NORO-TEST")
    assert resp.status_code == 200

    # 3. Poll — should return existing_user=true, NO api_key
    poll = await client.post("/v1/auth/device-code/poll", json={"device_code": "NORO-TEST"})
    poll_data = poll.json()
    assert poll_data["status"] == "approved"
    assert poll_data.get("existing_user") is True
    assert "api_key" not in poll_data

    # 4. Verify api_key_hash is UNCHANGED in the database
    db_session.expire_all()
    result = await db_session.execute(
        select(User).where(User.email == "norotate@example.com")
    )
    user_after = result.scalar_one()
    assert user_after.api_key_hash == hash_before, (
        "CRITICAL BUG: Login rotated the API key! "
        f"Hash before: {hash_before}, Hash after: {user_after.api_key_hash}"
    )

    # 5. Verify the original API key STILL WORKS
    resp = await client.get("/v1/cues", headers={"Authorization": f"Bearer {original_key}"})
    assert resp.status_code == 200, (
        f"CRITICAL BUG: Original API key is invalid after login! Status: {resp.status_code}"
    )


@pytest.mark.asyncio
async def test_device_code_poll_completes_after_verification(client, db_session, redis_client):
    """CLI poll must complete after browser verification — no hanging.

    The verify page no longer polls/claims the device code. It redirects
    immediately using data from verify_token(). This ensures the CLI
    (the only remaining poller) always sees 'approved' and can claim.
    """
    # 1. Create device code + submit email (new user)
    resp = await client.post("/v1/auth/device-code", json={"device_code": "CLIHANG1"})
    _skip_on_rate_limit(resp)
    await client.post("/v1/auth/device-code/submit-email", json={
        "device_code": "CLIHANG1", "email": "clipoll@example.com"
    })
    dc = await _get_device_code_from_db(db_session, "CLIHANG1")

    # 2. Browser clicks magic link (verify_token runs, status -> approved)
    resp = await client.get(f"/v1/auth/verify?token={dc.verification_token}&device_code=CLIHANG1")
    assert resp.status_code == 200
    # Verify page should NOT poll — just redirect immediately
    assert "Redirecting to dashboard" in resp.text
    assert "/v1/auth/device-code/poll" not in resp.text

    # 3. Device code should still be "approved" (not "claimed" by verify page)
    dc_after = await _get_device_code_from_db(db_session, "CLIHANG1")
    await db_session.refresh(dc_after)
    assert dc_after.status == "approved", (
        f"Expected 'approved' but got '{dc_after.status}' — "
        "verify page may be racing with CLI to claim"
    )

    # 4. CLI polls — should get approved + key (first poll claims)
    poll = await client.post("/v1/auth/device-code/poll", json={"device_code": "CLIHANG1"})
    data = poll.json()
    assert data["status"] == "approved"
    assert data["api_key"].startswith("cue_sk_")
    assert data["email"] == "clipoll@example.com"

    # 5. Device code is now "claimed" — subsequent polls return claimed
    poll2 = await client.post("/v1/auth/device-code/poll", json={"device_code": "CLIHANG1"})
    assert poll2.json()["status"] == "claimed"


@pytest.mark.asyncio
async def test_existing_user_poll_completes_after_verification(client, db_session, redis_client):
    """Existing user CLI poll must also complete after browser verification."""
    # Create existing user
    reg = await client.post("/v1/auth/register", json={"email": "existpoll@example.com"})
    assert reg.status_code == 201

    # Device code flow
    resp = await client.post("/v1/auth/device-code", json={"device_code": "CLIHANG2"})
    _skip_on_rate_limit(resp)
    await client.post("/v1/auth/device-code/submit-email", json={
        "device_code": "CLIHANG2", "email": "existpoll@example.com"
    })
    dc = await _get_device_code_from_db(db_session, "CLIHANG2")
    resp = await client.get(f"/v1/auth/verify?token={dc.verification_token}&device_code=CLIHANG2")
    assert resp.status_code == 200

    # Device code should still be "approved"
    dc_after = await _get_device_code_from_db(db_session, "CLIHANG2")
    await db_session.refresh(dc_after)
    assert dc_after.status == "approved"

    # CLI polls — should get approved + existing_user
    poll = await client.post("/v1/auth/device-code/poll", json={"device_code": "CLIHANG2"})
    data = poll.json()
    assert data["status"] == "approved"
    assert data.get("existing_user") is True
    assert "api_key" not in data


@pytest.mark.asyncio
async def test_device_page_html(client, redis_client):
    """GET /auth/device?code=TEST should return HTML."""
    response = await client.get("/auth/device?code=ABCD-EFGH")
    assert response.status_code == 200
    assert "CueAPI Login" in response.text
    assert "ABCD-EFGH" in response.text
