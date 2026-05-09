"""Operator-discoverability surface — `GET /v1/info`.

Exposes the substrate's static configuration so an operator can verify
"what's enforced" without grepping environment variables. Pairs with
`/health` (which covers runtime liveness): `/info` is about config,
`/health` is about state.

Per CWS-2026-05-08 Item 5 lock (operator-facing add): what's enforced
shouldn't be a grep-config exercise, especially for the
`AuthorizationBackend` hook where the active class determines whether
cross-user messaging is allowed at all.

No authentication required — same shape as `/health`. Returned info is
class names, environment-flag booleans, and version string; no secrets,
no per-User data, no per-Agent data.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from app.config import settings
from app.services.authorization_backend import get_authorization_backend

logger = logging.getLogger(__name__)

router = APIRouter(tags=["info"])

_VERSION_FILE = Path(__file__).resolve().parents[2] / "VERSION"


def _read_version() -> str:
    """Read the VERSION file; fall back to "unknown" on any read error."""
    try:
        return _VERSION_FILE.read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


def _disabled_primitives() -> list[str]:
    """Enumerate which primitives have been disabled via packaging knobs."""
    disabled: list[str] = []
    if settings.DISABLE_CUE_PRIMITIVE:
        disabled.append("cues")
    if settings.DISABLE_QUOTA_ENFORCEMENT:
        disabled.append("quotas")
    if settings.DISABLE_DEVICE_CODE:
        disabled.append("device_code")
    return disabled


def _authentication_paths() -> dict[str, bool]:
    """Report which authentication paths are reachable on this deployment."""
    return {
        # Path 1 is always-on — the User-row api_key_hash check is built-in.
        "per_user_api_key": True,
        # Path 2 only reachable when both the flag and the token are set.
        "internal_token_with_on_behalf_of": (
            settings.EXTERNAL_AUTH_BACKEND
            and bool(settings.INTERNAL_AUTH_TOKEN)
        ),
    }


@router.get("/v1/info")
async def get_info() -> dict[str, Any]:
    """Static configuration snapshot — version, active authz backend,
    authentication paths, disabled primitives.

    Intentionally does NOT include secrets (any token value, hook URL,
    DB password). Class names and boolean state only.
    """
    backend = get_authorization_backend()
    return {
        "version": _read_version(),
        "authorization_backend": type(backend).__name__,
        "authentication_paths": _authentication_paths(),
        "disabled_primitives": _disabled_primitives(),
    }
