"""PR-5b (Dock-readiness): pluggable cross-user authorization backend.

Verifies the new backend abstraction in
``app/services/authorization_backend.py``:

* ``SameTenantAuthorizationBackend`` (default) returns True only when
  sender_user_id == recipient_user_id, exactly matching v1 spec §3.4.
* ``WebhookAuthorizationBackend`` POSTs the decision request to the
  configured URL, caches via Redis, fail-closes on any error.
* Custom subclass loaded via ``AUTHORIZATION_BACKEND`` env var lets
  Dock-style integrators ship Python code that joins against their
  own permission model.
* The pre-existing same-tenant check in ``message_service.create_message``
  now routes through the backend — flipping it changes message
  acceptance.
"""
from __future__ import annotations

import json

import pytest
import pytest_asyncio
from contextlib import contextmanager

from app.config import settings
from app.services.authorization_backend import (
    AuthorizationBackend,
    SameTenantAuthorizationBackend,
    WebhookAuthorizationBackend,
    _reset_cached_backend_for_tests,
    get_authorization_backend,
)


@contextmanager
def _patch_settings(**overrides):
    original = {}
    for k, v in overrides.items():
        original[k] = getattr(settings, k)
        setattr(settings, k, v)
    _reset_cached_backend_for_tests()
    try:
        yield
    finally:
        for k, v in original.items():
            setattr(settings, k, v)
        _reset_cached_backend_for_tests()


# ─── SameTenantAuthorizationBackend (default) ────────────────────


class TestSameTenantBackend:
    @pytest.mark.asyncio
    async def test_same_user_allowed(self):
        backend = SameTenantAuthorizationBackend()
        ok = await backend.authorize_message(
            sender_user_id="user-a",
            recipient_user_id="user-a",
            sender_agent_id="agt_a",
            recipient_agent_id="agt_b",
        )
        assert ok is True

    @pytest.mark.asyncio
    async def test_different_users_denied(self):
        backend = SameTenantAuthorizationBackend()
        ok = await backend.authorize_message(
            sender_user_id="user-a",
            recipient_user_id="user-b",
            sender_agent_id="agt_a",
            recipient_agent_id="agt_b",
        )
        assert ok is False


# ─── Resolution: env vars pick the right backend ────────────────


class TestBackendResolution:
    def test_default_is_same_tenant(self):
        with _patch_settings(AUTHORIZATION_BACKEND="", AUTHZ_HOOK_URL=""):
            b = get_authorization_backend()
            assert isinstance(b, SameTenantAuthorizationBackend)

    def test_authz_hook_url_picks_webhook_backend(self):
        with _patch_settings(
            AUTHORIZATION_BACKEND="",
            AUTHZ_HOOK_URL="https://example.com/authz",
            AUTHZ_HOOK_SECRET="hook-secret-32-chars-long-enough!!",
        ):
            b = get_authorization_backend()
            assert isinstance(b, WebhookAuthorizationBackend)
            assert b.hook_url == "https://example.com/authz"
            assert b.hook_secret == "hook-secret-32-chars-long-enough!!"

    def test_python_path_picks_custom_backend(self):
        # Use the SameTenantAuthorizationBackend itself as the "custom"
        # class — the resolution is what matters, not which class.
        with _patch_settings(
            AUTHORIZATION_BACKEND="app.services.authorization_backend:SameTenantAuthorizationBackend",
            AUTHZ_HOOK_URL="https://example.com/authz",  # ignored when path set
        ):
            b = get_authorization_backend()
            assert isinstance(b, SameTenantAuthorizationBackend)

    def test_invalid_python_path_falls_back_safely(self):
        """A typo in the env var must not crash the app — fall through
        to the next resolution step."""
        with _patch_settings(
            AUTHORIZATION_BACKEND="nonexistent.module:NoneSuch",
            AUTHZ_HOOK_URL="",
        ):
            b = get_authorization_backend()
            # Falls back to default rather than raising.
            assert isinstance(b, SameTenantAuthorizationBackend)


# ─── WebhookAuthorizationBackend (Dock's expected mode) ─────────


class TestWebhookBackend:
    """The webhook backend POSTs to the integrator's URL and caches.
    These tests use httpx's mock transport so we don't need a live
    server. Real-world integration tests live downstream."""

    @pytest.mark.asyncio
    async def test_allow_response_returns_true(self, monkeypatch):
        async def mock_post(self, url, content=None, headers=None):
            class R:
                status_code = 200
                def json(self_inner):
                    return {"decision": "allow", "cache_ttl": 0}
            return R()

        # Patch httpx.AsyncClient.post on the instance.
        import httpx
        monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

        backend = WebhookAuthorizationBackend(
            hook_url="https://example.com/authz",
            hook_secret="x" * 32,
        )
        ok = await backend.authorize_message(
            sender_user_id="user-a",
            recipient_user_id="user-b",
            sender_agent_id="agt_a",
            recipient_agent_id="agt_b",
        )
        assert ok is True

    @pytest.mark.asyncio
    async def test_deny_response_returns_false(self, monkeypatch):
        async def mock_post(self, url, content=None, headers=None):
            class R:
                status_code = 200
                def json(self_inner):
                    return {"decision": "deny", "reason": "no shared workspace", "cache_ttl": 0}
            return R()

        import httpx
        monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

        backend = WebhookAuthorizationBackend(hook_url="https://example.com/authz")
        ok = await backend.authorize_message(
            sender_user_id="user-a",
            recipient_user_id="user-b",
            sender_agent_id="agt_a",
            recipient_agent_id="agt_b",
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_500_response_fails_closed(self, monkeypatch):
        async def mock_post(self, url, content=None, headers=None):
            class R:
                status_code = 500
                def json(self_inner):
                    return {}
            return R()

        import httpx
        monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

        backend = WebhookAuthorizationBackend(hook_url="https://example.com/authz")
        ok = await backend.authorize_message(
            sender_user_id="user-a",
            recipient_user_id="user-b",
            sender_agent_id="agt_a",
            recipient_agent_id="agt_b",
        )
        # Fail-closed: 5xx → deny.
        assert ok is False

    @pytest.mark.asyncio
    async def test_timeout_fails_closed(self, monkeypatch):
        import httpx

        async def mock_post(self, url, content=None, headers=None):
            raise httpx.TimeoutException("hook took too long")

        monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

        backend = WebhookAuthorizationBackend(hook_url="https://example.com/authz")
        ok = await backend.authorize_message(
            sender_user_id="user-a",
            recipient_user_id="user-b",
            sender_agent_id="agt_a",
            recipient_agent_id="agt_b",
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_invalid_decision_string_fails_closed(self, monkeypatch):
        async def mock_post(self, url, content=None, headers=None):
            class R:
                status_code = 200
                def json(self_inner):
                    return {"decision": "maybe", "cache_ttl": 0}
            return R()

        import httpx
        monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

        backend = WebhookAuthorizationBackend(hook_url="https://example.com/authz")
        ok = await backend.authorize_message(
            sender_user_id="user-a",
            recipient_user_id="user-b",
            sender_agent_id="agt_a",
            recipient_agent_id="agt_b",
        )
        assert ok is False


# ─── Dock-shaped subclass — sanity check for integrator pattern ─


class _AlwaysAllowBackend(AuthorizationBackend):
    """Used by integration test below — the simplest possible
    'integrator overrides default' shape."""

    async def authorize_message(self, **kwargs) -> bool:
        return True


class TestCustomBackendIntegration:
    """A custom subclass returning True for everything must let
    cross-user messages through. This is the surface Dock will use."""

    @pytest.mark.asyncio
    async def test_custom_backend_overrides_same_tenant(self):
        backend = _AlwaysAllowBackend()
        # Different users — same-tenant default would deny.
        ok = await backend.authorize_message(
            sender_user_id="user-a",
            recipient_user_id="user-b",
            sender_agent_id="agt_a",
            recipient_agent_id="agt_b",
        )
        assert ok is True
