"""Tests for the ``users.external_owner`` consumer-attribution field.

Per CWS-2026-05-08 Tier 2 lock (refined per CTO call): OSS
cueapi-core grows ``external_owner`` on the User row (single-key
shape; private cueapi grows it on api_keys, multi-key shape).

Field semantics (from migration 025 + model + endpoint surface):
* Type ``VARCHAR(64)`` NULL allowed
* Set via ``PUT /v1/internal/users/{id}`` ``external_owner`` body field
* NULL acceptable (self-mint via /v1/auth/register, or integrator
  omits the field)
* Audit-only — never used as auth/business-logic predicate

Pins
----

1. Upsert with ``external_owner`` set → field persists (created path)
2. Upsert without ``external_owner`` → NULL on the row
3. Update existing User with ``external_owner`` set → field updated
4. Update existing User with ``external_owner`` omitted (None) →
   existing value unchanged (preserves prior set)
5. Length cap 64 chars enforced by Pydantic surface
"""
from __future__ import annotations

import sys
import uuid
from contextlib import contextmanager

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.config import settings
from app.models import User


@contextmanager
def _patch_settings(**overrides):
    original = {}
    for k, v in overrides.items():
        original[k] = getattr(settings, k)
        setattr(settings, k, v)
    try:
        yield
    finally:
        for k, v in original.items():
            setattr(settings, k, v)


def _reimport_main():
    if "app.main" in sys.modules:
        del sys.modules["app.main"]
    import app.main  # noqa: F401
    return sys.modules["app.main"].app


# ─── 1. Create with external_owner ────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_creates_user_with_external_owner(db_session):
    """First upsert call sets external_owner on the new User row."""
    with _patch_settings(
        EXTERNAL_AUTH_BACKEND=True,
        INTERNAL_AUTH_TOKEN="test-internal-token-32chars-min!!",
    ):
        app = _reimport_main()
        transport = ASGITransport(app=app)
        user_id = str(uuid.uuid4())
        slug = f"ext-{uuid.uuid4().hex[:8]}"
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.put(
                f"/v1/internal/users/{user_id}",
                headers={"Authorization": "Bearer test-internal-token-32chars-min!!"},
                json={
                    "email": "extowner@test.com",
                    "slug": slug,
                    "external_owner": "dock",
                },
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["created"] is True

        # Verify the field persisted on the row
        row = (
            await db_session.execute(select(User).where(User.id == uuid.UUID(user_id)))
        ).scalar_one()
        assert row.external_owner == "dock"


# ─── 2. Create without external_owner → NULL ─────────────────────


@pytest.mark.asyncio
async def test_upsert_creates_user_with_null_external_owner(db_session):
    """Omitting external_owner from the body → NULL on the row.
    Confirms the field is optional, not required."""
    with _patch_settings(
        EXTERNAL_AUTH_BACKEND=True,
        INTERNAL_AUTH_TOKEN="test-internal-token-32chars-min!!",
    ):
        app = _reimport_main()
        transport = ASGITransport(app=app)
        user_id = str(uuid.uuid4())
        slug = f"null-ext-{uuid.uuid4().hex[:8]}"
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.put(
                f"/v1/internal/users/{user_id}",
                headers={"Authorization": "Bearer test-internal-token-32chars-min!!"},
                json={"email": "nullext@test.com", "slug": slug},
            )
            assert resp.status_code == 200, resp.text

        row = (
            await db_session.execute(select(User).where(User.id == uuid.UUID(user_id)))
        ).scalar_one()
        assert row.external_owner is None


# ─── 3. Update with external_owner → field updated ────────────────


@pytest.mark.asyncio
async def test_upsert_updates_external_owner_on_existing_user(db_session):
    """Subsequent upsert with a different external_owner overwrites
    the existing value. Idempotent semantic."""
    with _patch_settings(
        EXTERNAL_AUTH_BACKEND=True,
        INTERNAL_AUTH_TOKEN="test-internal-token-32chars-min!!",
    ):
        app = _reimport_main()
        transport = ASGITransport(app=app)
        user_id = str(uuid.uuid4())
        slug = f"upd-{uuid.uuid4().hex[:8]}"
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Create with external_owner=dock
            resp1 = await client.put(
                f"/v1/internal/users/{user_id}",
                headers={"Authorization": "Bearer test-internal-token-32chars-min!!"},
                json={
                    "email": "upd@test.com",
                    "slug": slug,
                    "external_owner": "dock",
                },
            )
            assert resp1.status_code == 200
            # Update to external_owner=integrator-x
            resp2 = await client.put(
                f"/v1/internal/users/{user_id}",
                headers={"Authorization": "Bearer test-internal-token-32chars-min!!"},
                json={
                    "email": "upd@test.com",
                    "slug": slug,
                    "external_owner": "integrator-x",
                },
            )
            assert resp2.status_code == 200
            assert resp2.json()["created"] is False

        db_session.expire_all()
        row = (
            await db_session.execute(select(User).where(User.id == uuid.UUID(user_id)))
        ).scalar_one()
        assert row.external_owner == "integrator-x"


# ─── 4. Update without external_owner → preserves prior value ────


@pytest.mark.asyncio
async def test_upsert_omitted_external_owner_preserves_prior(db_session):
    """If external_owner is omitted on update, the prior value stays.
    Mirrors the per-field-on-update semantic for plan/limits/etc."""
    with _patch_settings(
        EXTERNAL_AUTH_BACKEND=True,
        INTERNAL_AUTH_TOKEN="test-internal-token-32chars-min!!",
    ):
        app = _reimport_main()
        transport = ASGITransport(app=app)
        user_id = str(uuid.uuid4())
        slug = f"preserve-{uuid.uuid4().hex[:8]}"
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Create with external_owner=dock
            resp1 = await client.put(
                f"/v1/internal/users/{user_id}",
                headers={"Authorization": "Bearer test-internal-token-32chars-min!!"},
                json={
                    "email": "preserve@test.com",
                    "slug": slug,
                    "external_owner": "dock",
                },
            )
            assert resp1.status_code == 200
            # Update WITHOUT external_owner field — should preserve "dock"
            resp2 = await client.put(
                f"/v1/internal/users/{user_id}",
                headers={"Authorization": "Bearer test-internal-token-32chars-min!!"},
                json={
                    "email": "preserve-updated@test.com",
                    "slug": slug,
                    "plan": "pro",  # change something else
                },
            )
            assert resp2.status_code == 200

        db_session.expire_all()
        row = (
            await db_session.execute(select(User).where(User.id == uuid.UUID(user_id)))
        ).scalar_one()
        assert row.external_owner == "dock"
        assert row.email == "preserve-updated@test.com"
        assert row.plan == "pro"


# ─── 5. Length cap enforced ───────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_rejects_external_owner_over_64_chars(db_session):
    """Pydantic surface caps at 64 chars (matches schema). Long
    values rejected at validation time, not at DB-INSERT time."""
    with _patch_settings(
        EXTERNAL_AUTH_BACKEND=True,
        INTERNAL_AUTH_TOKEN="test-internal-token-32chars-min!!",
    ):
        app = _reimport_main()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.put(
                f"/v1/internal/users/{uuid.uuid4()}",
                headers={"Authorization": "Bearer test-internal-token-32chars-min!!"},
                json={
                    "email": "long@test.com",
                    "slug": "long-ext",
                    "external_owner": "a" * 65,  # one over cap
                },
            )
            assert resp.status_code == 422
