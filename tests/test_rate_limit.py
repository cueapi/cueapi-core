from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_rate_limit_returns_headers(client, auth_headers, redis_client):
    """Every authenticated response should include rate limit headers."""
    response = await client.get("/v1/cues", headers=auth_headers)
    assert response.status_code == 200
    assert "x-ratelimit-limit" in response.headers
    assert "x-ratelimit-remaining" in response.headers


@pytest.mark.asyncio
async def test_rate_limit_blocks_at_limit(client, auth_headers, redis_client):
    """Free tier: 60 req/min. 61st request should return 429."""
    for i in range(60):
        resp = await client.get("/v1/cues", headers=auth_headers)
        assert resp.status_code == 200, f"Request {i+1} failed with {resp.status_code}"

    resp = await client.get("/v1/cues", headers=auth_headers)
    assert resp.status_code == 429
    assert "retry-after" in resp.headers


@pytest.mark.asyncio
async def test_rate_limit_works_across_endpoints(client, auth_headers, redis_client):
    """Rate limit is per API key, not per endpoint."""
    for i in range(30):
        await client.get("/v1/cues", headers=auth_headers)
    for i in range(30):
        await client.get("/v1/usage", headers=auth_headers)

    resp = await client.get("/v1/cues", headers=auth_headers)
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_health_endpoint_not_rate_limited(client, redis_client):
    """Health endpoint should be exempt from rate limiting."""
    for i in range(100):
        resp = await client.get("/health")
        assert resp.status_code == 200
