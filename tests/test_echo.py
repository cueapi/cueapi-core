from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_echo_store_and_retrieve(client, auth_headers, redis_client):
    token = "test-echo-token-0123456789"

    # Store (no auth)
    resp = await client.post(f"/v1/echo/{token}", json={"hello": "world"})
    assert resp.status_code == 200
    assert resp.json()["stored"] is True

    # Retrieve (auth required)
    resp = await client.get(f"/v1/echo/{token}", headers=auth_headers)
    assert resp.json()["status"] == "delivered"
    assert resp.json()["payload"] == {"hello": "world"}
    assert "received_at" in resp.json()


@pytest.mark.asyncio
async def test_echo_waiting(client, auth_headers, redis_client):
    resp = await client.get("/v1/echo/nonexistent", headers=auth_headers)
    assert resp.json()["status"] == "waiting"


@pytest.mark.asyncio
async def test_echo_requires_auth_for_retrieve(client, redis_client):
    """GET /v1/echo/{token} requires auth."""
    resp = await client.get("/v1/echo/some-token")
    assert resp.status_code == 401
