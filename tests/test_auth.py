import hashlib

import pytest


@pytest.mark.asyncio
async def test_register_returns_api_key(client):
    response = await client.post("/v1/auth/register", json={"email": "test@example.com"})
    assert response.status_code == 201
    data = response.json()
    assert data["api_key"].startswith("cue_sk_")
    assert data["email"] == "test@example.com"


@pytest.mark.asyncio
async def test_register_duplicate_email_fails(client):
    await client.post("/v1/auth/register", json={"email": "dupe@example.com"})
    response = await client.post("/v1/auth/register", json={"email": "dupe@example.com"})
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_invalid_api_key_returns_401(client):
    response = await client.get("/v1/cues", headers={"Authorization": "Bearer bad_key"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_missing_auth_returns_401_or_403(client):
    response = await client.get("/v1/cues")
    assert response.status_code in [401, 403]


@pytest.mark.asyncio
async def test_valid_api_key_authenticates(client, registered_user):
    api_key = registered_user["api_key"]
    response = await client.get("/v1/cues", headers={"Authorization": f"Bearer {api_key}"})
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_auth_cache_populated(client, registered_user, redis_client):
    api_key = registered_user["api_key"]
    await client.get("/v1/cues", headers={"Authorization": f"Bearer {api_key}"})
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    cached = await redis_client.get(f"auth:{key_hash}")
    assert cached is not None
