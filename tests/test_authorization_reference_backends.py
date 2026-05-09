"""Reference AuthorizationBackend implementations.

Verifies the two importable-reference backends in
``app/services/authorization_backend.py``:

* ``EveryoneAuthorizationBackend`` — always allows.
* ``AllowlistAuthorizationBackend`` — allows same-user sends, plus a
  directed (sender_user, recipient_user) allowlist parsed from either
  the ``AUTHZ_ALLOWLIST`` env var or the constructor.

Both are importable via ``from app.services.authorization_backend
import ...`` and activatable via the ``AUTHORIZATION_BACKEND`` env var
import-path mechanism.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from app.config import settings
from app.services.authorization_backend import (
    AllowlistAuthorizationBackend,
    AuthorizationBackend,
    EveryoneAuthorizationBackend,
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


# ─── EveryoneAuthorizationBackend ────────────────────────────────


class TestEveryoneBackend:
    @pytest.mark.asyncio
    async def test_same_user_allowed(self):
        backend = EveryoneAuthorizationBackend()
        ok = await backend.authorize_message(
            sender_user_id="user-a",
            recipient_user_id="user-a",
            sender_agent_id="agt_a",
            recipient_agent_id="agt_b",
        )
        assert ok is True

    @pytest.mark.asyncio
    async def test_cross_user_allowed(self):
        backend = EveryoneAuthorizationBackend()
        ok = await backend.authorize_message(
            sender_user_id="user-a",
            recipient_user_id="user-b",
            sender_agent_id="agt_a",
            recipient_agent_id="agt_b",
        )
        assert ok is True

    @pytest.mark.asyncio
    async def test_loads_via_env_resolution(self):
        with _patch_settings(
            AUTHORIZATION_BACKEND=(
                "app.services.authorization_backend:EveryoneAuthorizationBackend"
            ),
        ):
            backend = get_authorization_backend()
            assert isinstance(backend, EveryoneAuthorizationBackend)
            assert isinstance(backend, AuthorizationBackend)

    def test_subclasses_authorization_backend(self):
        assert issubclass(EveryoneAuthorizationBackend, AuthorizationBackend)


# ─── AllowlistAuthorizationBackend ───────────────────────────────


class TestAllowlistBackendDirectInit:
    @pytest.mark.asyncio
    async def test_same_user_always_allowed_even_with_empty_allowlist(self):
        backend = AllowlistAuthorizationBackend(allowed_pairs=set())
        ok = await backend.authorize_message(
            sender_user_id="user-a",
            recipient_user_id="user-a",
            sender_agent_id="agt_a",
            recipient_agent_id="agt_b",
        )
        assert ok is True

    @pytest.mark.asyncio
    async def test_cross_user_allowed_when_in_allowlist(self):
        backend = AllowlistAuthorizationBackend(
            allowed_pairs={("user-a", "user-b")},
        )
        ok = await backend.authorize_message(
            sender_user_id="user-a",
            recipient_user_id="user-b",
            sender_agent_id="agt_a",
            recipient_agent_id="agt_b",
        )
        assert ok is True

    @pytest.mark.asyncio
    async def test_cross_user_denied_when_not_in_allowlist(self):
        backend = AllowlistAuthorizationBackend(
            allowed_pairs={("user-a", "user-b")},
        )
        ok = await backend.authorize_message(
            sender_user_id="user-a",
            recipient_user_id="user-c",  # not in allowlist
            sender_agent_id="agt_a",
            recipient_agent_id="agt_c",
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_pairs_are_directional(self):
        # A → B configured; B → A should NOT be allowed automatically.
        backend = AllowlistAuthorizationBackend(
            allowed_pairs={("user-a", "user-b")},
        )
        forward = await backend.authorize_message(
            sender_user_id="user-a",
            recipient_user_id="user-b",
            sender_agent_id="agt_a",
            recipient_agent_id="agt_b",
        )
        reverse = await backend.authorize_message(
            sender_user_id="user-b",
            recipient_user_id="user-a",
            sender_agent_id="agt_b",
            recipient_agent_id="agt_a",
        )
        assert forward is True
        assert reverse is False


class TestAllowlistBackendEnvParse:
    def test_parses_single_pair(self):
        pairs = AllowlistAuthorizationBackend._parse_env_allowlist("user-a:user-b")
        assert pairs == [("user-a", "user-b")]

    def test_parses_multiple_pairs(self):
        pairs = AllowlistAuthorizationBackend._parse_env_allowlist(
            "user-a:user-b,user-c:user-d"
        )
        assert pairs == [("user-a", "user-b"), ("user-c", "user-d")]

    def test_strips_whitespace_around_tokens(self):
        pairs = AllowlistAuthorizationBackend._parse_env_allowlist(
            "  user-a : user-b  ,  user-c:user-d  "
        )
        assert pairs == [("user-a", "user-b"), ("user-c", "user-d")]

    def test_empty_string_returns_empty_list(self):
        assert AllowlistAuthorizationBackend._parse_env_allowlist("") == []

    def test_skips_malformed_entry_missing_colon(self):
        # No colon → skip with warning, don't crash. Other entries survive.
        pairs = AllowlistAuthorizationBackend._parse_env_allowlist(
            "user-a:user-b,malformed,user-c:user-d"
        )
        assert pairs == [("user-a", "user-b"), ("user-c", "user-d")]

    def test_skips_entry_with_empty_half(self):
        pairs = AllowlistAuthorizationBackend._parse_env_allowlist(
            "user-a:user-b,:user-c,user-d:,user-e:user-f"
        )
        assert pairs == [("user-a", "user-b"), ("user-e", "user-f")]


class TestAllowlistBackendEnvIntegration:
    @pytest.mark.asyncio
    async def test_no_args_reads_env(self):
        with _patch_settings(AUTHZ_ALLOWLIST="user-a:user-b"):
            backend = AllowlistAuthorizationBackend()
            allowed = await backend.authorize_message(
                sender_user_id="user-a",
                recipient_user_id="user-b",
                sender_agent_id="agt_a",
                recipient_agent_id="agt_b",
            )
            denied = await backend.authorize_message(
                sender_user_id="user-a",
                recipient_user_id="user-c",
                sender_agent_id="agt_a",
                recipient_agent_id="agt_c",
            )
            assert allowed is True
            assert denied is False

    @pytest.mark.asyncio
    async def test_loads_via_env_resolution_with_allowlist(self):
        with _patch_settings(
            AUTHORIZATION_BACKEND=(
                "app.services.authorization_backend:AllowlistAuthorizationBackend"
            ),
            AUTHZ_ALLOWLIST="user-a:user-b",
        ):
            backend = get_authorization_backend()
            assert isinstance(backend, AllowlistAuthorizationBackend)
            ok = await backend.authorize_message(
                sender_user_id="user-a",
                recipient_user_id="user-b",
                sender_agent_id="agt_a",
                recipient_agent_id="agt_b",
            )
            assert ok is True

    @pytest.mark.asyncio
    async def test_explicit_pairs_override_env(self):
        # Constructor arg wins over env var — env-driven path is opt-in.
        with _patch_settings(AUTHZ_ALLOWLIST="user-a:user-b"):
            backend = AllowlistAuthorizationBackend(
                allowed_pairs={("user-x", "user-y")},
            )
            env_pair_blocked = await backend.authorize_message(
                sender_user_id="user-a",
                recipient_user_id="user-b",
                sender_agent_id="agt_a",
                recipient_agent_id="agt_b",
            )
            explicit_pair_allowed = await backend.authorize_message(
                sender_user_id="user-x",
                recipient_user_id="user-y",
                sender_agent_id="agt_x",
                recipient_agent_id="agt_y",
            )
            assert env_pair_blocked is False
            assert explicit_pair_allowed is True

    def test_subclasses_authorization_backend(self):
        assert issubclass(AllowlistAuthorizationBackend, AuthorizationBackend)
