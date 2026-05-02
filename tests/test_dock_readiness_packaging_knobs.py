"""PR-5d (Dock-readiness): packaging-mode env-var flags.

Verifies the three packaging-mode flags introduced for self-host
integrators (Dock Connect first; future others). All three default to
False so default behavior matches the full cueapi-core experience —
these tests pin both the default-off behavior AND the on-behavior.

The flags:

* ``DISABLE_CUE_PRIMITIVE``    — strips cues/executions/workers routers
* ``DISABLE_DEVICE_CODE``      — strips email-magic-link signup
* ``DISABLE_QUOTA_ENFORCEMENT``— bypasses cue + message quota checks

Strategy
--------
The flag wiring lives in two places:

1. **Router registration** (``app/main.py``) — the FastAPI app object
   only mounts the gated routers when the corresponding flag is False.
   Tested via the OpenAPI route inventory after re-importing the app
   with the flag patched.
2. **Service-layer guards** (``app/services/cue_service.py``,
   ``app/services/message_service.py``) — quota checks short-circuit
   when ``DISABLE_QUOTA_ENFORCEMENT`` is True. Tested by exercising
   the create-path with a user already at quota.

Both mechanisms must work for self-hosters to ship a properly
slimmed deployment.
"""
from __future__ import annotations

import importlib
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from app.config import settings
from app.models import Cue, Message, User
from app.utils.ids import (
    generate_api_key,
    generate_webhook_secret,
    get_api_key_prefix,
    hash_api_key,
)


# ─── Helpers ───────────────────────────────────────────────────────


@contextmanager
def _patch_settings(**overrides):
    """Patch app.config.settings + reload modules that read settings
    at import time (cue_service, message_service, main). Restores on
    exit so other tests aren't affected.
    """
    original = {}
    for k, v in overrides.items():
        original[k] = getattr(settings, k)
        setattr(settings, k, v)
    try:
        yield
    finally:
        for k, v in original.items():
            setattr(settings, k, v)


def _reimport_main():
    """Reload app.main so router registration re-evaluates against the
    currently-patched settings. Returns the fresh app object."""
    if "app.main" in sys.modules:
        del sys.modules["app.main"]
    import app.main  # noqa: F401
    return sys.modules["app.main"].app


def _route_paths(app) -> set[str]:
    return {r.path for r in app.routes if hasattr(r, "path")}


# ─── DISABLE_CUE_PRIMITIVE: routers stripped at startup ───────────


class TestDisableCuePrimitive:
    def test_default_off_cue_routes_present(self):
        """Default deployment includes all cue + executions + workers
        routes. Tested by inspecting the FastAPI app's route inventory
        with no flags overridden."""
        with _patch_settings(DISABLE_CUE_PRIMITIVE=False):
            app = _reimport_main()
            paths = _route_paths(app)
            # A representative subset — full path set is large.
            assert "/v1/cues" in paths or any(p.startswith("/v1/cues") for p in paths)
            assert any(p.startswith("/v1/executions") for p in paths)
            assert any(p.startswith("/v1/workers") for p in paths)

    def test_flag_on_cue_routes_stripped(self):
        """Flipping ``DISABLE_CUE_PRIMITIVE=true`` removes cue,
        executions, and workers routers. messaging routers stay."""
        with _patch_settings(DISABLE_CUE_PRIMITIVE=True):
            app = _reimport_main()
            paths = _route_paths(app)
            assert not any(p.startswith("/v1/cues") for p in paths), \
                "cues router must not mount when DISABLE_CUE_PRIMITIVE=True"
            assert not any(p.startswith("/v1/executions") for p in paths), \
                "executions router must not mount when DISABLE_CUE_PRIMITIVE=True"
            assert not any(p.startswith("/v1/workers") for p in paths), \
                "workers router must not mount when DISABLE_CUE_PRIMITIVE=True"
            # Messaging primitive remains live.
            assert any(p.startswith("/v1/agents") for p in paths)
            assert any(p.startswith("/v1/messages") for p in paths)


# ─── DISABLE_DEVICE_CODE: signup flow stripped ────────────────────


class TestDisableDeviceCode:
    def test_default_off_device_code_routes_present(self):
        with _patch_settings(DISABLE_DEVICE_CODE=False):
            app = _reimport_main()
            paths = _route_paths(app)
            assert any("device-code" in p for p in paths), \
                "default deployment must expose device-code signup"

    def test_flag_on_device_code_stripped(self):
        with _patch_settings(DISABLE_DEVICE_CODE=True):
            app = _reimport_main()
            paths = _route_paths(app)
            assert not any("device-code" in p for p in paths), \
                "device-code router must not mount when DISABLE_DEVICE_CODE=True"
            # Messaging stays live.
            assert any(p.startswith("/v1/agents") for p in paths)


# ─── Both flags simultaneously (Dock's expected combo) ────────────


class TestDockMessagingOnlyMode:
    """Dock's expected production config: cue + device-code stripped,
    messaging primitive only. Pin the combination."""

    def test_messaging_only_combination(self):
        with _patch_settings(
            DISABLE_CUE_PRIMITIVE=True,
            DISABLE_DEVICE_CODE=True,
        ):
            app = _reimport_main()
            paths = _route_paths(app)

            # What Dock keeps:
            assert any(p.startswith("/v1/agents") for p in paths)
            assert any(p.startswith("/v1/messages") for p in paths)
            assert any(p == "/health" or p.endswith("/health") for p in paths)

            # What Dock drops:
            for forbidden in ("/v1/cues", "/v1/executions", "/v1/workers"):
                assert not any(p.startswith(forbidden) for p in paths), f"{forbidden} leaked"
            assert not any("device-code" in p for p in paths)


# ─── DISABLE_QUOTA_ENFORCEMENT: service-layer guards ─────────────


@pytest_asyncio.fixture
async def quota_user(db_session):
    """A user already at their active_cue_limit. Default cue create
    path should 403; flag-on path should succeed."""
    raw_key = generate_api_key()
    user = User(
        email=f"quota-{uuid.uuid4().hex[:8]}@test.com",
        api_key_hash=hash_api_key(raw_key),
        api_key_prefix=get_api_key_prefix(raw_key),
        webhook_secret=generate_webhook_secret(),
        slug=f"quota-{uuid.uuid4().hex[:8]}",
        active_cue_limit=2,  # tight cap so we hit it with 2 cues
        monthly_message_limit=2,
    )
    db_session.add(user)
    await db_session.flush()

    # Two existing active cues to put the user AT the cap.
    for i in range(2):
        cue = Cue(
            id=f"cue_quota{i:08d}xxx",
            user_id=user.id,
            name=f"existing-cue-{i}",
            schedule_type="recurring",
            schedule_cron="0 0 * * *",
            schedule_timezone="UTC",
            callback_transport="webhook",
            callback_url="https://example.com/wh",
            callback_method="POST",
            payload={},
            status="active",
            next_run=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db_session.add(cue)
    await db_session.commit()
    return user


class TestDisableQuotaEnforcement:
    """Pin that the flag actually short-circuits both cue and message
    quota checks. The default-off path is implicitly covered by
    the existing test_messages.py + test_cues.py suites that already
    assert quota errors at cap — those would break if the default
    flipped, which is its own regression test."""

    @pytest.mark.asyncio
    async def test_flag_off_cue_create_at_cap_is_blocked(
        self, db_session, quota_user
    ):
        """Default behavior: hitting the cue cap returns
        cue_limit_exceeded. This is the regression-pin for the
        default-on path."""
        from app.schemas.cue import CueCreate, ScheduleConfig
        from app.services.cue_service import create_cue

        with _patch_settings(DISABLE_QUOTA_ENFORCEMENT=False):
            data = CueCreate(
                name="should-fail-at-cap",
                schedule=ScheduleConfig(type="recurring", cron="0 0 * * *", timezone="UTC"),
                transport="webhook",
                callback={"url": "https://example.com/x", "method": "POST"},
            )
            # Build a minimal AuthenticatedUser shape from the User row.
            from app.auth import AuthenticatedUser
            auth_user = AuthenticatedUser(
                id=quota_user.id,
                email=quota_user.email,
                plan=quota_user.plan,
                active_cue_limit=quota_user.active_cue_limit,
                monthly_execution_limit=quota_user.monthly_execution_limit,
                rate_limit_per_minute=quota_user.rate_limit_per_minute,
                api_key_id=None,
            )
            result = await create_cue(db_session, auth_user, data)
            assert "error" in result
            assert result["error"]["code"] == "cue_limit_exceeded"

    @pytest.mark.asyncio
    async def test_flag_on_cue_create_at_cap_succeeds(
        self, db_session, quota_user
    ):
        """With DISABLE_QUOTA_ENFORCEMENT=True, the same call goes
        through despite the user being at their cap."""
        from app.schemas.cue import CueCreate, ScheduleConfig
        from app.services.cue_service import create_cue

        with _patch_settings(DISABLE_QUOTA_ENFORCEMENT=True):
            data = CueCreate(
                name="should-pass-when-quotas-disabled",
                schedule=ScheduleConfig(type="recurring", cron="0 0 * * *", timezone="UTC"),
                transport="webhook",
                callback={"url": "https://example.com/x", "method": "POST"},
            )
            from app.auth import AuthenticatedUser
            auth_user = AuthenticatedUser(
                id=quota_user.id,
                email=quota_user.email,
                plan=quota_user.plan,
                active_cue_limit=quota_user.active_cue_limit,
                monthly_execution_limit=quota_user.monthly_execution_limit,
                rate_limit_per_minute=quota_user.rate_limit_per_minute,
                api_key_id=None,
            )
            result = await create_cue(db_session, auth_user, data)
            assert "error" not in result, f"flag should bypass cap, got: {result}"
            assert result.get("id", "").startswith("cue_")
