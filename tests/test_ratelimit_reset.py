from __future__ import annotations

import time

import pytest


@pytest.mark.asyncio
async def test_ratelimit_reset_header_on_normal_response(client, auth_headers, redis_client):
    """Every authenticated response should include X-RateLimit-Reset header."""
    response = await client.get("/v1/cues", headers=auth_headers)
    assert response.status_code == 200
    assert "x-ratelimit-reset" in response.headers
    reset = int(response.headers["x-ratelimit-reset"])
    now = int(time.time())
    # Reset should be roughly 60s from now (within 5s tolerance)
    assert now + 55 <= reset <= now + 65


@pytest.mark.asyncio
async def test_ratelimit_reset_header_on_429(client, auth_headers, redis_client):
    """429 responses should include X-RateLimit-Reset header."""
    # Exhaust rate limit (free tier = 60)
    for _ in range(60):
        await client.get("/v1/cues", headers=auth_headers)

    resp = await client.get("/v1/cues", headers=auth_headers)
    assert resp.status_code == 429
    assert "x-ratelimit-reset" in resp.headers
    reset = int(resp.headers["x-ratelimit-reset"])
    now = int(time.time())
    # Reset should be a unix epoch in the future (within the next 60s)
    assert now <= reset <= now + 65


@pytest.mark.asyncio
async def test_ratelimit_headers_always_present(client, auth_headers, redis_client):
    """X-RateLimit-Limit, X-RateLimit-Remaining, and X-RateLimit-Reset should all be on every response."""
    response = await client.get("/v1/usage", headers=auth_headers)
    assert response.status_code == 200
    assert "x-ratelimit-limit" in response.headers
    assert "x-ratelimit-remaining" in response.headers
    assert "x-ratelimit-reset" in response.headers
