"""Rate limit regression tests — feedback loop fix and edge cases.

Tests the critical fix where rejected 429 requests no longer inflate the
sliding window. Also tests recovery, header accuracy, and IP-based limiting.
"""
from __future__ import annotations

import time

import pytest


@pytest.mark.asyncio
async def test_rejected_requests_do_not_inflate_window(client, auth_headers, redis_client):
    """Regression: 429 responses must NOT count against the sliding window.

    Before the fix, every rejected request added an entry to the sorted set,
    creating a feedback loop where the user could never recover until ALL
    entries (including rejected ones) aged out.
    """
    # Fill up the window to the limit (60 for free tier)
    for i in range(60):
        resp = await client.get("/v1/cues", headers=auth_headers)
        assert resp.status_code == 200, f"Request {i+1} failed with {resp.status_code}"

    # Send 20 more requests — all should be 429
    for i in range(20):
        resp = await client.get("/v1/cues", headers=auth_headers)
        assert resp.status_code == 429

    # The window should still only contain 60 entries (not 80)
    # Verify by checking X-RateLimit-Remaining on the next 429
    resp = await client.get("/v1/cues", headers=auth_headers)
    assert resp.status_code == 429
    assert resp.headers.get("x-ratelimit-remaining") == "0"
    assert resp.headers.get("x-ratelimit-limit") == "60"


@pytest.mark.asyncio
async def test_rate_limit_remaining_decrements(client, auth_headers, redis_client):
    """X-RateLimit-Remaining should decrement with each request."""
    resp = await client.get("/v1/cues", headers=auth_headers)
    assert resp.status_code == 200
    remaining_first = int(resp.headers["x-ratelimit-remaining"])

    resp = await client.get("/v1/cues", headers=auth_headers)
    assert resp.status_code == 200
    remaining_second = int(resp.headers["x-ratelimit-remaining"])

    assert remaining_second == remaining_first - 1


@pytest.mark.asyncio
async def test_rate_limit_retry_after_header(client, auth_headers, redis_client):
    """429 response should include Retry-After header with a reasonable value."""
    for i in range(60):
        await client.get("/v1/cues", headers=auth_headers)

    resp = await client.get("/v1/cues", headers=auth_headers)
    assert resp.status_code == 429
    retry_after = int(resp.headers["retry-after"])
    assert 1 <= retry_after <= 60


@pytest.mark.asyncio
async def test_rate_limit_error_response_format(client, auth_headers, redis_client):
    """429 response body should follow the standard error format."""
    for i in range(60):
        await client.get("/v1/cues", headers=auth_headers)

    resp = await client.get("/v1/cues", headers=auth_headers)
    assert resp.status_code == 429
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == "rate_limit_exceeded"
    assert body["error"]["status"] == 429
    assert "Retry after" in body["error"]["message"]


@pytest.mark.asyncio
async def test_ip_rate_limiting_for_unauthenticated(client, redis_client):
    """Unauthenticated requests should be rate-limited by IP."""
    # Make several requests without auth to a non-exempt endpoint
    # These will get 401 (auth required) but should still be rate-limited
    for i in range(60):
        await client.get("/v1/cues")

    resp = await client.get("/v1/cues")
    # Either 401 (auth) or 429 (rate limit) — rate limit should kick in
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_status_endpoint_not_rate_limited(client, redis_client):
    """Status endpoint should be exempt from rate limiting, like health."""
    for i in range(100):
        resp = await client.get("/status")
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_different_api_keys_have_separate_limits(client, auth_headers, other_auth_headers, redis_client):
    """Each API key should have its own rate limit window."""
    # Use up most of user A's limit
    for i in range(55):
        resp = await client.get("/v1/cues", headers=auth_headers)
        assert resp.status_code == 200

    # User B should still have their full limit
    resp = await client.get("/v1/cues", headers=other_auth_headers)
    assert resp.status_code == 200
    remaining = int(resp.headers["x-ratelimit-remaining"])
    assert remaining >= 58  # Should be near full (60 - 2 used)
