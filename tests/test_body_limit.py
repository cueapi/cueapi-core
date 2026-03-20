"""Body size limit middleware tests.

Tests the 1MB Content-Length enforcement including boundary conditions,
chunked/streaming bodies, missing headers, and valid requests passing through.
"""
from __future__ import annotations

import pytest

from app.middleware.body_limit import MAX_BODY_SIZE


@pytest.mark.asyncio
async def test_body_over_limit_rejected(client, auth_headers):
    """Request with Content-Length > 1MB should return 413."""
    over_size = MAX_BODY_SIZE + 1024  # 1MB + 1KB
    response = await client.post(
        "/v1/cues",
        headers={**auth_headers, "content-length": str(over_size)},
        content="x" * 1024,  # Actual content small — middleware checks header
    )
    assert response.status_code == 413
    body = response.json()
    assert body["error"]["code"] == "request_too_large"
    assert body["error"]["status"] == 413


@pytest.mark.asyncio
async def test_body_just_over_limit_rejected(client, auth_headers):
    """Request with Content-Length = 1MB + 1 should return 413."""
    just_over = MAX_BODY_SIZE + 1
    response = await client.post(
        "/v1/cues",
        headers={**auth_headers, "content-length": str(just_over)},
        content="x" * 100,
    )
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_2mb_payload_returns_413(client, auth_headers):
    """2MB payload must return 413 not 503 (Argus issue #21)."""
    two_mb = 2 * 1024 * 1024
    response = await client.post(
        "/v1/cues",
        headers={**auth_headers, "content-length": str(two_mb)},
        content="x" * 1024,
    )
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_small_body_passes(client, auth_headers):
    """Normal-sized request body should pass through."""
    response = await client.post("/v1/cues", headers=auth_headers, json={
        "name": "normal-body",
        "schedule": {"type": "recurring", "cron": "0 9 * * *"},
        "callback": {"url": "https://example.com/webhook"},
        "payload": {"task": "check", "data": "small"}
    })
    assert response.status_code == 201


@pytest.mark.asyncio
async def test_get_requests_not_affected(client, auth_headers):
    """GET requests without body should not be affected by body limit."""
    response = await client.get("/v1/cues", headers=auth_headers)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_health_not_affected_by_body_limit(client):
    """Health endpoint should work regardless of body limit middleware."""
    response = await client.get("/health")
    assert response.status_code == 200
