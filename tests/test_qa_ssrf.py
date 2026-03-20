"""QA Phase 1.3 — Security Hardening Tests.

Tests for SSRF protection, body size limits, auth rate limiting, and echo hardening.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
import pytest_asyncio

from tests.conftest import client, auth_headers, registered_user, redis_client  # noqa: F401


# --- SSRF Protection Tests ---


@pytest.mark.asyncio
async def test_ssrf_block_localhost(client, auth_headers):
    """Bug 1: Callback URL targeting localhost must be blocked."""
    resp = await client.post(
        "/v1/cues",
        json={
            "name": "ssrf-localhost",
            "schedule": {"type": "once", "at": "2099-01-01T00:00:00Z"},
            "callback": {"url": "https://localhost/hook"},
        },
        headers=auth_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_callback_url"


@pytest.mark.asyncio
async def test_ssrf_block_127(client, auth_headers):
    """Bug 1: Callback URL targeting 127.0.0.1 must be blocked."""
    resp = await client.post(
        "/v1/cues",
        json={
            "name": "ssrf-127",
            "schedule": {"type": "once", "at": "2099-01-01T00:00:00Z"},
            "callback": {"url": "https://127.0.0.1/hook"},
        },
        headers=auth_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_callback_url"


@pytest.mark.asyncio
async def test_ssrf_block_10_network(client, auth_headers):
    """Bug 1: Callback URL targeting 10.x.x.x must be blocked."""
    with patch("app.utils.url_validation.socket.getaddrinfo") as mock_resolve:
        mock_resolve.return_value = [(2, 1, 6, "", ("10.0.0.1", 0))]
        resp = await client.post(
            "/v1/cues",
            json={
                "name": "ssrf-10",
                "schedule": {"type": "once", "at": "2099-01-01T00:00:00Z"},
                "callback": {"url": "https://evil.com/hook"},
            },
            headers=auth_headers,
        )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_callback_url"


@pytest.mark.asyncio
async def test_ssrf_block_172_16_network(client, auth_headers):
    """Bug 1: Callback URL targeting 172.16.x.x must be blocked."""
    with patch("app.utils.url_validation.socket.getaddrinfo") as mock_resolve:
        mock_resolve.return_value = [(2, 1, 6, "", ("172.16.0.1", 0))]
        resp = await client.post(
            "/v1/cues",
            json={
                "name": "ssrf-172",
                "schedule": {"type": "once", "at": "2099-01-01T00:00:00Z"},
                "callback": {"url": "https://evil.com/hook"},
            },
            headers=auth_headers,
        )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_callback_url"


@pytest.mark.asyncio
async def test_ssrf_block_192_168_network(client, auth_headers):
    """Bug 1: Callback URL targeting 192.168.x.x must be blocked."""
    with patch("app.utils.url_validation.socket.getaddrinfo") as mock_resolve:
        mock_resolve.return_value = [(2, 1, 6, "", ("192.168.1.1", 0))]
        resp = await client.post(
            "/v1/cues",
            json={
                "name": "ssrf-192",
                "schedule": {"type": "once", "at": "2099-01-01T00:00:00Z"},
                "callback": {"url": "https://evil.com/hook"},
            },
            headers=auth_headers,
        )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_callback_url"


@pytest.mark.asyncio
async def test_ssrf_block_metadata(client, auth_headers):
    """Bug 1: Callback URL targeting cloud metadata endpoint must be blocked."""
    with patch("app.utils.url_validation.socket.getaddrinfo") as mock_resolve:
        mock_resolve.return_value = [(2, 1, 6, "", ("169.254.169.254", 0))]
        resp = await client.post(
            "/v1/cues",
            json={
                "name": "ssrf-metadata",
                "schedule": {"type": "once", "at": "2099-01-01T00:00:00Z"},
                "callback": {"url": "https://metadata.google.internal/computeMetadata/v1/"},
            },
            headers=auth_headers,
        )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_callback_url"


@pytest.mark.asyncio
async def test_ssrf_allow_valid_url(client, auth_headers):
    """Bug 1: Valid public URLs must still be accepted."""
    resp = await client.post(
        "/v1/cues",
        json={
            "name": "ssrf-valid",
            "schedule": {"type": "once", "at": "2099-01-01T00:00:00Z"},
            "callback": {"url": "https://example.com/webhook"},
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201


# --- Body Size Limit Test ---


@pytest.mark.asyncio
async def test_oversized_request_rejected(client, auth_headers):
    """Bug 2: Request > 2MB must be rejected before parsing."""
    big_payload = "x" * (3 * 1024 * 1024)  # 3MB
    resp = await client.post(
        "/v1/cues",
        content=big_payload,
        headers={**auth_headers, "Content-Type": "application/json", "Content-Length": str(len(big_payload))},
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "request_too_large"


# --- Auth Rate Limiting Tests ---


@pytest.mark.asyncio
async def test_device_code_rate_limit(client, redis_client):
    """Bug 3: Device code creation must be rate limited (5/IP/hour)."""
    for i in range(5):
        resp = await client.post(
            "/v1/auth/device-code",
            json={"device_code": f"CODE{i:04d}AB"},
        )
        # Could be 201 or 409 (duplicate), but not 429
        assert resp.status_code != 429

    # 6th request should be rate limited
    resp = await client.post(
        "/v1/auth/device-code",
        json={"device_code": "CODE0005AB"},
    )
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "rate_limit_exceeded"


@pytest.mark.asyncio
async def test_magic_link_rate_limit(client, redis_client):
    """Bug 3: Magic link must be rate limited (3/email/hour)."""
    # First create a device code
    await client.post("/v1/auth/device-code", json={"device_code": "MLTEST01AB"})

    email = "ratelimit@test.com"
    for i in range(3):
        resp = await client.post(
            "/v1/auth/device-code/submit-email",
            json={"device_code": "MLTEST01AB", "email": email},
        )
        # Should not be 429 yet
        assert resp.status_code != 429

    # 4th request should be rate limited
    resp = await client.post(
        "/v1/auth/device-code/submit-email",
        json={"device_code": "MLTEST01AB", "email": email},
    )
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "rate_limit_exceeded"


# --- Credential Leak Protection Tests ---


@pytest.mark.asyncio
async def test_ssrf_block_credentials_in_url(client, auth_headers):
    """Callback URLs with embedded credentials (user:pass@host) must be rejected."""
    resp = await client.post(
        "/v1/cues",
        json={
            "name": "cred-leak",
            "schedule": {"type": "once", "at": "2099-01-01T00:00:00Z"},
            "callback": {"url": "https://user:pass@example.com/webhook"},
        },
        headers=auth_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_callback_url"
    assert "Credentials" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_ssrf_block_username_only_in_url(client, auth_headers):
    """Callback URLs with username-only (user@host) must also be rejected."""
    resp = await client.post(
        "/v1/cues",
        json={
            "name": "cred-leak-user",
            "schedule": {"type": "once", "at": "2099-01-01T00:00:00Z"},
            "callback": {"url": "https://admin@example.com/webhook"},
        },
        headers=auth_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_callback_url"
