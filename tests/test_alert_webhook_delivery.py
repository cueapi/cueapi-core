"""Alert webhook delivery: HMAC signing, SSRF, timeouts, failure
handling, no-URL short-circuit."""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alert import Alert
from app.services.alert_webhook import deliver_alert


def _fake_alert() -> Alert:
    a = Alert(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        cue_id="cue_x",
        execution_id=uuid.uuid4(),
        alert_type="verification_failed",
        severity="warning",
        message="m",
        alert_metadata={"k": "v"},
        acknowledged=False,
    )
    a.created_at = datetime.now(timezone.utc)
    return a


class TestDeliverAlertShortCircuits:
    @pytest.mark.asyncio
    async def test_no_url_returns_false_silently(self):
        a = _fake_alert()
        ok = await deliver_alert(a, alert_webhook_url=None, alert_webhook_secret="x" * 64)
        assert ok is False

    @pytest.mark.asyncio
    async def test_url_without_secret_skipped(self):
        a = _fake_alert()
        ok = await deliver_alert(a, alert_webhook_url="https://example.com", alert_webhook_secret=None)
        assert ok is False


class TestSSRF:
    @pytest.mark.asyncio
    async def test_ssrf_blocks_private_ip(self):
        a = _fake_alert()
        # 127.0.0.1 is blocked in production-style checks. Use the
        # production path explicitly.
        with patch("app.services.alert_webhook.settings.ENV", "production"):
            ok = await deliver_alert(
                a,
                alert_webhook_url="http://127.0.0.1/hook",
                alert_webhook_secret="s" * 64,
            )
        assert ok is False


class TestHMACSignature:
    @pytest.mark.asyncio
    async def test_signature_header_present_and_correct(self):
        a = _fake_alert()
        secret = "a" * 64

        captured = {}

        async def _fake_post(self, url, headers=None, content=None, **kw):
            captured["url"] = url
            captured["headers"] = dict(headers or {})
            captured["content"] = content
            resp = MagicMock()
            resp.status_code = 200
            return resp

        # Bypass SSRF for this test
        with patch(
            "app.services.alert_webhook.validate_url_at_delivery",
            return_value=(True, ""),
        ), patch("httpx.AsyncClient.post", new=_fake_post):
            ok = await deliver_alert(
                a,
                alert_webhook_url="https://example.com/hook",
                alert_webhook_secret=secret,
            )
        assert ok is True

        sig_header = captured["headers"].get("X-CueAPI-Signature")
        ts_header = captured["headers"].get("X-CueAPI-Timestamp")
        assert sig_header and sig_header.startswith("v1=")
        assert ts_header and ts_header.isdigit()
        assert captured["headers"].get("X-CueAPI-Alert-Id") == str(a.id)
        assert captured["headers"].get("X-CueAPI-Alert-Type") == "verification_failed"

        # Recompute signature over "{ts}.{sorted_payload}" and compare.
        signed = f"{ts_header}.".encode() + captured["content"].encode()
        expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        assert sig_header == f"v1={expected}"


class TestFailureModes:
    @pytest.mark.asyncio
    async def test_timeout_returns_false(self):
        import httpx

        a = _fake_alert()

        async def _boom(self, url, **kw):
            raise httpx.TimeoutException("too slow")

        with patch(
            "app.services.alert_webhook.validate_url_at_delivery",
            return_value=(True, ""),
        ), patch("httpx.AsyncClient.post", new=_boom):
            ok = await deliver_alert(
                a,
                alert_webhook_url="https://example.com",
                alert_webhook_secret="s" * 64,
            )
        assert ok is False  # did not raise

    @pytest.mark.asyncio
    async def test_non_2xx_returns_false(self):
        a = _fake_alert()

        async def _fail(self, url, **kw):
            resp = MagicMock()
            resp.status_code = 500
            return resp

        with patch(
            "app.services.alert_webhook.validate_url_at_delivery",
            return_value=(True, ""),
        ), patch("httpx.AsyncClient.post", new=_fail):
            ok = await deliver_alert(
                a,
                alert_webhook_url="https://example.com",
                alert_webhook_secret="s" * 64,
            )
        assert ok is False

    @pytest.mark.asyncio
    async def test_unexpected_exception_swallowed(self):
        a = _fake_alert()

        async def _boom(self, url, **kw):
            raise RuntimeError("surprise")

        with patch(
            "app.services.alert_webhook.validate_url_at_delivery",
            return_value=(True, ""),
        ), patch("httpx.AsyncClient.post", new=_boom):
            ok = await deliver_alert(
                a,
                alert_webhook_url="https://example.com",
                alert_webhook_secret="s" * 64,
            )
        assert ok is False
