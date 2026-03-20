"""URL validation edge cases — IPv6, unusual schemes, encoding bypasses.

Supplements test_qa_ssrf.py with additional SSRF protection tests.
"""
from __future__ import annotations

import pytest

from app.utils.url_validation import validate_callback_url


# ---- IPv6 tests ----

def test_block_ipv6_loopback():
    """IPv6 loopback [::1] should be blocked."""
    is_valid, msg = validate_callback_url("https://[::1]/webhook")
    assert not is_valid
    assert "blocked" in msg.lower() or "resolve" in msg.lower()


def test_block_ipv6_private_fc00():
    """IPv6 private range fc00::/7 should be blocked."""
    is_valid, msg = validate_callback_url("https://[fc00::1]/webhook")
    assert not is_valid


def test_block_ipv6_link_local():
    """IPv6 link-local fe80::/10 should be blocked."""
    is_valid, msg = validate_callback_url("https://[fe80::1]/webhook")
    assert not is_valid


# ---- Scheme tests ----

def test_block_ftp_scheme():
    """FTP scheme should be rejected."""
    is_valid, msg = validate_callback_url("ftp://example.com/webhook")
    assert not is_valid
    assert "http" in msg.lower() or "scheme" in msg.lower()


def test_block_file_scheme():
    """file:// scheme should be rejected."""
    is_valid, msg = validate_callback_url("file:///etc/passwd")
    assert not is_valid


def test_block_javascript_scheme():
    """javascript: scheme should be rejected."""
    is_valid, msg = validate_callback_url("javascript:alert(1)")
    assert not is_valid


def test_block_data_scheme():
    """data: scheme should be rejected."""
    is_valid, msg = validate_callback_url("data:text/html,<h1>hi</h1>")
    assert not is_valid


# ---- HTTP in production vs development ----

def test_block_http_in_production():
    """HTTP should be rejected in production (default)."""
    is_valid, msg = validate_callback_url("http://example.com/webhook", env="production")
    assert not is_valid
    assert "https" in msg.lower()


def test_allow_http_in_development():
    """HTTP should be allowed in development."""
    is_valid, msg = validate_callback_url("http://example.com/webhook", env="development")
    # May still fail on DNS resolution in test, but scheme check should pass
    # We only check the scheme is accepted, not the DNS resolution
    # If example.com resolves, it should be valid
    assert is_valid or "resolve" in msg.lower()


def test_allow_https_in_production():
    """HTTPS should be accepted in production."""
    is_valid, msg = validate_callback_url("https://example.com/webhook", env="production")
    assert is_valid


# ---- Hostname edge cases ----

def test_block_metadata_google_internal():
    """Cloud metadata hostname should be blocked."""
    is_valid, msg = validate_callback_url("https://metadata.google.internal/computeMetadata/v1/")
    assert not is_valid
    assert "blocked" in msg.lower() or "hostname" in msg.lower()


def test_block_metadata_internal():
    """Short metadata hostname should be blocked."""
    is_valid, msg = validate_callback_url("https://metadata.internal/")
    assert not is_valid


def test_block_localhost_uppercase():
    """LOCALHOST (case insensitive) should be blocked."""
    is_valid, msg = validate_callback_url("https://LOCALHOST/webhook")
    assert not is_valid


def test_block_localhost_mixed_case():
    """LocalHost (mixed case) should be blocked."""
    is_valid, msg = validate_callback_url("https://LocalHost/webhook")
    assert not is_valid


# ---- Credential edge cases ----

def test_block_credentials_with_special_chars():
    """Credentials with special characters should be blocked."""
    is_valid, msg = validate_callback_url("https://user%40:p%40ss@example.com/webhook")
    assert not is_valid
    assert "credential" in msg.lower() or "resolve" in msg.lower()


# ---- No hostname ----

def test_block_no_hostname():
    """URL without hostname should be rejected."""
    is_valid, msg = validate_callback_url("https:///webhook")
    assert not is_valid
    assert "hostname" in msg.lower()


# ---- Valid URLs ----

def test_accept_valid_https():
    """Standard HTTPS URL should pass."""
    is_valid, msg = validate_callback_url("https://example.com/webhook")
    assert is_valid
    assert msg == ""


def test_accept_https_with_port():
    """HTTPS URL with port should pass."""
    is_valid, msg = validate_callback_url("https://example.com:8443/webhook")
    assert is_valid


def test_accept_https_with_path_and_query():
    """HTTPS URL with path and query params should pass."""
    is_valid, msg = validate_callback_url("https://example.com/v1/hooks?token=abc123")
    assert is_valid


def test_accept_subdomain():
    """HTTPS with subdomain should pass."""
    is_valid, msg = validate_callback_url("https://www.google.com/webhook")
    assert is_valid


# ---- Integration: callback URL rejection via API ----

@pytest.mark.asyncio
async def test_api_rejects_ipv6_loopback_callback(client, auth_headers):
    """Creating a cue with IPv6 loopback callback should be rejected."""
    resp = await client.post("/v1/cues", headers=auth_headers, json={
        "name": "ipv6-ssrf",
        "schedule": {"type": "recurring", "cron": "0 9 * * *"},
        "callback": {"url": "https://[::1]/webhook"}
    })
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_callback_url"


@pytest.mark.asyncio
async def test_api_rejects_ftp_callback(client, auth_headers):
    """Creating a cue with FTP callback should be rejected."""
    resp = await client.post("/v1/cues", headers=auth_headers, json={
        "name": "ftp-ssrf",
        "schedule": {"type": "recurring", "cron": "0 9 * * *"},
        "callback": {"url": "ftp://example.com/data"}
    })
    # Pydantic HttpUrl validation will catch this before our SSRF check
    assert resp.status_code in (400, 422)
