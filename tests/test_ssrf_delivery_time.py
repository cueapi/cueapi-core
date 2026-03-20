"""Tests for delivery-time SSRF validation (DNS rebind protection).

Validates that deliver_webhook() re-checks hostname resolution at delivery
time and blocks requests to private/internal IPs even if the URL passed
creation-time validation.

Tests use env="production" explicitly to test blocking behavior, since the
test environment is "development" which allows localhost for local webhook tests.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.services.webhook import deliver_webhook
from app.utils.url_validation import validate_url_at_delivery, is_blocked_ip


# ── is_blocked_ip unit tests ────────────────────────────────────

def test_blocked_ip_loopback():
    assert is_blocked_ip("127.0.0.1") is True


def test_blocked_ip_private_10():
    assert is_blocked_ip("10.0.0.1") is True


def test_blocked_ip_private_172():
    assert is_blocked_ip("172.16.0.1") is True


def test_blocked_ip_private_192():
    assert is_blocked_ip("192.168.1.1") is True


def test_blocked_ip_metadata():
    assert is_blocked_ip("169.254.169.254") is True


def test_blocked_ip_ipv6_loopback():
    assert is_blocked_ip("::1") is True


def test_allowed_ip_public():
    assert is_blocked_ip("93.184.216.34") is False


def test_blocked_ip_invalid():
    """Unparseable IP strings are treated as blocked."""
    assert is_blocked_ip("not-an-ip") is True


# ── validate_url_at_delivery unit tests (production mode) ───────

def test_delivery_blocks_localhost_in_production():
    valid, error = validate_url_at_delivery("https://localhost/hook", env="production")
    assert not valid
    assert "blocked" in error.lower()


def test_delivery_allows_localhost_in_development():
    """In dev, localhost is allowed for local webhook testing."""
    valid, error = validate_url_at_delivery("http://localhost:9999/webhook", env="development")
    assert valid


def test_delivery_blocks_metadata_hostname():
    valid, error = validate_url_at_delivery("https://metadata.google.internal/v1/", env="production")
    assert not valid
    assert "blocked" in error.lower()


def test_delivery_blocks_metadata_in_dev():
    """Cloud metadata is blocked even in development."""
    with patch("app.utils.url_validation.socket.getaddrinfo") as mock_dns:
        mock_dns.return_value = [(2, 1, 6, "", ("169.254.169.254", 0))]
        valid, error = validate_url_at_delivery("https://evil.com/hook", env="development")
    assert not valid


def test_delivery_blocks_dns_rebind():
    """Hostname that resolved to public at creation but to private at delivery."""
    with patch("app.utils.url_validation.socket.getaddrinfo") as mock_dns:
        mock_dns.return_value = [(2, 1, 6, "", ("10.0.0.1", 0))]
        valid, error = validate_url_at_delivery("https://attacker.com/hook", env="production")
    assert not valid
    assert "blocked" in error.lower()


def test_delivery_allows_public_url():
    valid, error = validate_url_at_delivery("https://example.com/webhook", env="production")
    assert valid
    assert error == ""


def test_delivery_blocks_unresolvable():
    with patch("app.utils.url_validation.socket.getaddrinfo") as mock_dns:
        mock_dns.side_effect = __import__("socket").gaierror("nxdomain")
        valid, error = validate_url_at_delivery("https://nonexistent.invalid/hook", env="production")
    assert not valid


# ── deliver_webhook integration tests ───────────────────────────

@pytest.mark.asyncio
async def test_deliver_webhook_blocks_ssrf_at_delivery():
    """deliver_webhook() must reject a URL that resolves to private IP in production."""
    with patch("app.services.webhook.settings") as mock_settings:
        mock_settings.ENV = "production"
        mock_settings.WEBHOOK_TIMEOUT_SECONDS = 30
        with patch("app.utils.url_validation.socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [(2, 1, 6, "", ("10.0.0.1", 0))]
            success, status, body = await deliver_webhook(
                callback_url="https://attacker.com/hook",
                callback_method="POST",
                callback_headers={},
                payload={"test": True},
                cue_id="cue-ssrf-test",
                cue_name="ssrf-test",
                execution_id="exec-ssrf-test",
                scheduled_for=datetime.now(timezone.utc),
                attempt=1,
                webhook_secret="test-secret",
            )
    assert success is False
    assert status is None
    assert "blocked" in (body or "").lower()


@pytest.mark.asyncio
async def test_deliver_webhook_blocks_localhost_in_production():
    """deliver_webhook() must reject localhost URLs in production."""
    with patch("app.services.webhook.settings") as mock_settings:
        mock_settings.ENV = "production"
        mock_settings.WEBHOOK_TIMEOUT_SECONDS = 30
        success, status, body = await deliver_webhook(
            callback_url="https://localhost/hook",
            callback_method="POST",
            callback_headers={},
            payload={"test": True},
            cue_id="cue-localhost-test",
            cue_name="localhost-test",
            execution_id="exec-localhost-test",
            scheduled_for=datetime.now(timezone.utc),
            attempt=1,
            webhook_secret="test-secret",
        )
    assert success is False
    assert status is None
    assert "blocked" in (body or "").lower()


@pytest.mark.asyncio
async def test_deliver_webhook_no_follow_redirects():
    """deliver_webhook() should not follow redirects (redirect = non-2xx)."""
    from unittest.mock import AsyncMock, MagicMock

    mock_response = MagicMock()
    mock_response.status_code = 302
    mock_response.text = "Redirecting..."

    with patch("app.services.webhook.validate_url_at_delivery", return_value=(True, "")):
        with patch("httpx.AsyncClient") as MockClient:
            mock_client_instance = AsyncMock()
            mock_client_instance.post = AsyncMock(return_value=mock_response)
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_client_instance

            success, status, body = await deliver_webhook(
                callback_url="https://example.com/hook",
                callback_method="POST",
                callback_headers={},
                payload={},
                cue_id="cue-redirect",
                cue_name="redirect-test",
                execution_id="exec-redirect",
                scheduled_for=datetime.now(timezone.utc),
                attempt=1,
                webhook_secret="test-secret",
            )

            # 302 is not 2xx — should be treated as failure
            assert success is False
            assert status == 302

            # Verify follow_redirects=False was passed
            MockClient.assert_called_once()
            call_kwargs = MockClient.call_args
            assert call_kwargs.kwargs.get("follow_redirects") is False
