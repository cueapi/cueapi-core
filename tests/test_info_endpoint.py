"""Operator-discoverability surface — `GET /v1/info`.

Verifies the `/v1/info` endpoint reports:

* The static version string (read from VERSION file at process start).
* The active `AuthorizationBackend` class name.
* Which authentication paths are reachable on this deployment.
* Which primitives have been disabled via packaging knobs.

Per CWS-2026-05-08 Item 5 lock (operator-facing add). No-auth endpoint;
secrets are never returned.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from app.config import settings
from app.routers.info import _read_version
from app.services.authorization_backend import _reset_cached_backend_for_tests


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


class TestInfoEndpoint:
    @pytest.mark.asyncio
    async def test_returns_200_no_auth(self, client):
        # Endpoint accepts requests with no auth header.
        resp = await client.get("/v1/info")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_response_shape(self, client):
        resp = await client.get("/v1/info")
        body = resp.json()
        # Required top-level keys.
        assert set(body.keys()) == {
            "version",
            "authorization_backend",
            "authentication_paths",
            "disabled_primitives",
        }
        # Types.
        assert isinstance(body["version"], str)
        assert isinstance(body["authorization_backend"], str)
        assert isinstance(body["authentication_paths"], dict)
        assert isinstance(body["disabled_primitives"], list)

    @pytest.mark.asyncio
    async def test_default_authorization_backend(self, client):
        # Default deployment runs SameTenantAuthorizationBackend.
        with _patch_settings(AUTHORIZATION_BACKEND="", AUTHZ_HOOK_URL=""):
            resp = await client.get("/v1/info")
            assert resp.json()["authorization_backend"] == (
                "SameTenantAuthorizationBackend"
            )

    @pytest.mark.asyncio
    async def test_custom_authorization_backend_via_env(self, client):
        # Explicit AUTHORIZATION_BACKEND import path resolves and is
        # reported by name. Uses SameTenantAuthorizationBackend (the
        # default class) explicitly via env var to verify the resolver
        # path; reference-backend imports are exercised in
        # test_authorization_reference_backends.py.
        with _patch_settings(
            AUTHORIZATION_BACKEND=(
                "app.services.authorization_backend:SameTenantAuthorizationBackend"
            ),
        ):
            resp = await client.get("/v1/info")
            assert resp.json()["authorization_backend"] == (
                "SameTenantAuthorizationBackend"
            )

    @pytest.mark.asyncio
    async def test_authentication_paths_path1_always_true(self, client):
        # Path 1 (per-User API key) is always reachable.
        resp = await client.get("/v1/info")
        assert resp.json()["authentication_paths"]["per_user_api_key"] is True

    @pytest.mark.asyncio
    async def test_authentication_paths_path2_off_by_default(self, client):
        with _patch_settings(EXTERNAL_AUTH_BACKEND=False, INTERNAL_AUTH_TOKEN=""):
            resp = await client.get("/v1/info")
            assert resp.json()["authentication_paths"][
                "internal_token_with_on_behalf_of"
            ] is False

    @pytest.mark.asyncio
    async def test_authentication_paths_path2_requires_both_flag_and_token(self, client):
        # Flag set but token empty → still off (correctly fail-closed).
        with _patch_settings(
            EXTERNAL_AUTH_BACKEND=True,
            INTERNAL_AUTH_TOKEN="",
        ):
            resp = await client.get("/v1/info")
            assert resp.json()["authentication_paths"][
                "internal_token_with_on_behalf_of"
            ] is False

    @pytest.mark.asyncio
    async def test_authentication_paths_path2_on_when_configured(self, client):
        with _patch_settings(
            EXTERNAL_AUTH_BACKEND=True,
            INTERNAL_AUTH_TOKEN="test-internal-token-value",
        ):
            resp = await client.get("/v1/info")
            assert resp.json()["authentication_paths"][
                "internal_token_with_on_behalf_of"
            ] is True

    @pytest.mark.asyncio
    async def test_disabled_primitives_empty_by_default(self, client):
        with _patch_settings(
            DISABLE_CUE_PRIMITIVE=False,
            DISABLE_QUOTA_ENFORCEMENT=False,
            DISABLE_DEVICE_CODE=False,
        ):
            resp = await client.get("/v1/info")
            assert resp.json()["disabled_primitives"] == []

    @pytest.mark.asyncio
    async def test_disabled_primitives_reports_each_flag(self, client):
        with _patch_settings(
            DISABLE_CUE_PRIMITIVE=True,
            DISABLE_QUOTA_ENFORCEMENT=True,
            DISABLE_DEVICE_CODE=True,
        ):
            resp = await client.get("/v1/info")
            disabled = set(resp.json()["disabled_primitives"])
            assert disabled == {"cues", "quotas", "device_code"}

    @pytest.mark.asyncio
    async def test_no_secrets_in_response(self, client):
        # Set every secret-shaped env var; none should leak into the response.
        with _patch_settings(
            INTERNAL_AUTH_TOKEN="secret-internal-token",
            AUTHZ_HOOK_SECRET="secret-hook-key",
            AUTHZ_HOOK_URL="https://authz-hook.example/check",
            AUTHZ_ALLOWLIST="user-a:user-b",
        ):
            resp = await client.get("/v1/info")
            body_text = resp.text
            assert "secret-internal-token" not in body_text
            assert "secret-hook-key" not in body_text
            assert "authz-hook.example" not in body_text
            assert "user-a:user-b" not in body_text


class TestVersionFile:
    def test_read_version_returns_stripped_string(self):
        # The VERSION file at repo root has at least a non-empty version.
        version = _read_version()
        assert isinstance(version, str)
        assert len(version) > 0
        assert "\n" not in version
        assert version != "unknown" or not version.endswith(" ")  # sanity
