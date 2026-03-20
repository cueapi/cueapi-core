from __future__ import annotations

import hashlib

import pytest

from app.utils.ids import hash_api_key


@pytest.mark.asyncio
async def test_regenerate_key_returns_new_key(client, auth_headers, redis_client):
    """Key regeneration should return a new cue_sk_ key."""
    response = await client.post("/v1/auth/key/regenerate", headers={**auth_headers, "X-Confirm-Destructive": "true"})
    assert response.status_code == 200
    data = response.json()
    assert data["api_key"].startswith("cue_sk_")
    assert data["previous_key_revoked"] is True


@pytest.mark.asyncio
async def test_old_key_invalid_after_regeneration(client, auth_headers, redis_client):
    """Old key should be rejected after regeneration."""
    resp = await client.post("/v1/auth/key/regenerate", headers={**auth_headers, "X-Confirm-Destructive": "true"})
    new_key = resp.json()["api_key"]

    # Old key should fail
    response = await client.get("/v1/cues", headers=auth_headers)
    assert response.status_code == 401

    # New key should work
    response = await client.get("/v1/cues", headers={"Authorization": f"Bearer {new_key}"})
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_regenerate_clears_old_cache(client, auth_headers, redis_client):
    """Old auth cache should be deleted after regeneration."""
    old_key = auth_headers["Authorization"].replace("Bearer ", "")
    old_hash = hash_api_key(old_key)

    # Make a request to ensure cache is populated
    await client.get("/v1/cues", headers=auth_headers)
    cached = await redis_client.get(f"auth:{old_hash}")
    assert cached is not None

    # Regenerate
    await client.post("/v1/auth/key/regenerate", headers={**auth_headers, "X-Confirm-Destructive": "true"})

    # Old cache should be gone
    cached = await redis_client.get(f"auth:{old_hash}")
    assert cached is None
