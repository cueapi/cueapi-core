"""Phase 13 — Per-User Webhook Secrets tests."""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.user import User
from app.utils.signing import sign_payload, verify_signature


# ── Webhook secret on registration ──────────────────────────────────


@pytest.mark.asyncio
async def test_register_generates_webhook_secret(client, db_session):
    """Registration creates a user with a whsec_ webhook secret."""
    email = f"wh-{uuid.uuid4().hex[:8]}@test.com"
    resp = await client.post("/v1/auth/register", json={"email": email})
    assert resp.status_code == 201

    result = await db_session.execute(
        select(User.webhook_secret).where(User.email == email)
    )
    secret = result.scalar_one()
    assert secret.startswith("whsec_")
    assert len(secret) == 70  # "whsec_" + 64 hex chars


@pytest.mark.asyncio
async def test_each_user_gets_unique_secret(client, db_session):
    """Two different users get different webhook secrets."""
    email1 = f"wh1-{uuid.uuid4().hex[:8]}@test.com"
    email2 = f"wh2-{uuid.uuid4().hex[:8]}@test.com"
    await client.post("/v1/auth/register", json={"email": email1})
    await client.post("/v1/auth/register", json={"email": email2})

    r1 = await db_session.execute(select(User.webhook_secret).where(User.email == email1))
    r2 = await db_session.execute(select(User.webhook_secret).where(User.email == email2))
    assert r1.scalar_one() != r2.scalar_one()


# ── GET /v1/auth/webhook-secret ─────────────────────────────────────


@pytest.mark.asyncio
async def test_get_webhook_secret(client, auth_headers, db_session, registered_user):
    """GET /v1/auth/webhook-secret returns the user's secret."""
    resp = await client.get("/v1/auth/webhook-secret", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["webhook_secret"].startswith("whsec_")

    # Verify it matches the DB
    result = await db_session.execute(
        select(User.webhook_secret).where(User.email == registered_user["email"])
    )
    db_secret = result.scalar_one()
    assert data["webhook_secret"] == db_secret


@pytest.mark.asyncio
async def test_get_webhook_secret_requires_auth(client):
    """GET /v1/auth/webhook-secret without auth returns 401."""
    resp = await client.get("/v1/auth/webhook-secret")
    assert resp.status_code == 401


# ── POST /v1/auth/webhook-secret/regenerate ─────────────────────────


@pytest.mark.asyncio
async def test_regenerate_webhook_secret(client, auth_headers, db_session, registered_user):
    """Regeneration returns a new secret and revokes the old one."""
    # Get original secret
    resp1 = await client.get("/v1/auth/webhook-secret", headers=auth_headers)
    original_secret = resp1.json()["webhook_secret"]

    # Regenerate
    resp2 = await client.post("/v1/auth/webhook-secret/regenerate", headers={**auth_headers, "X-Confirm-Destructive": "true"})
    assert resp2.status_code == 200
    data = resp2.json()
    assert data["webhook_secret"].startswith("whsec_")
    assert data["previous_secret_revoked"] is True
    assert data["webhook_secret"] != original_secret

    # Verify DB has the new secret
    result = await db_session.execute(
        select(User.webhook_secret).where(User.email == registered_user["email"])
    )
    assert result.scalar_one() == data["webhook_secret"]


@pytest.mark.asyncio
async def test_regenerate_requires_auth(client):
    """POST /v1/auth/webhook-secret/regenerate without auth returns 401."""
    resp = await client.post("/v1/auth/webhook-secret/regenerate")
    assert resp.status_code == 401


# ── /v1/auth/me includes has_webhook_secret ─────────────────────────


@pytest.mark.asyncio
async def test_me_includes_has_webhook_secret(client, auth_headers):
    """GET /v1/auth/me response includes has_webhook_secret: true."""
    resp = await client.get("/v1/auth/me", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["has_webhook_secret"] is True


# ── Signing utility ─────────────────────────────────────────────────


def test_sign_payload_returns_timestamp_and_v1_signature():
    """sign_payload returns (timestamp, 'v1=...' signature)."""
    payload = {"cue_id": "cue_abc", "payload": {"task": "test"}}
    ts, sig = sign_payload(payload, "whsec_test_secret")
    assert ts.isdigit()
    assert sig.startswith("v1=")
    assert len(sig) > 4  # v1= + hex


def test_verify_signature_valid():
    """verify_signature returns True for valid signature."""
    payload = {"key": "value"}
    secret = "whsec_my_secret_123"
    ts, sig = sign_payload(payload, secret)
    assert verify_signature(payload, secret, ts, sig) is True


def test_verify_signature_wrong_secret():
    """verify_signature returns False with wrong secret."""
    payload = {"key": "value"}
    ts, sig = sign_payload(payload, "whsec_real_secret")
    assert verify_signature(payload, "whsec_wrong_secret", ts, sig) is False


def test_verify_signature_expired():
    """verify_signature returns False if timestamp is too old."""
    payload = {"key": "value"}
    secret = "whsec_my_secret"
    old_ts = str(int(time.time()) - 600)  # 10 minutes ago
    # Manually compute what sign_payload would have done
    import hashlib
    import hmac
    import json

    payload_bytes = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    signed_content = f"{old_ts}.".encode("utf-8") + payload_bytes
    signature = hmac.new(
        secret.encode("utf-8"), signed_content, hashlib.sha256
    ).hexdigest()
    sig = f"v1={signature}"

    # Valid signature but expired (default tolerance is 300s)
    assert verify_signature(payload, secret, old_ts, sig, tolerance_seconds=300) is False


def test_verify_signature_tampered_payload():
    """verify_signature returns False if payload was modified."""
    payload = {"key": "value"}
    secret = "whsec_my_secret"
    ts, sig = sign_payload(payload, secret)

    tampered = {"key": "tampered"}
    assert verify_signature(tampered, secret, ts, sig) is False
