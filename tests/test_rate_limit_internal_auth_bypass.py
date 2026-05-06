"""Tests for the trusted-proxy bypass in ``RateLimitMiddleware``.

Operators deploying cueapi-core behind a proxy that fans out per-user
requests under a single shared outbound IP (e.g. Vercel's egress pool,
a Fly.io app routing many tenants through one instance) hit the
default 60/min per-IP cap immediately because every user lands in the
same bucket.

The bypass piggybacks on the existing ``INTERNAL_AUTH_TOKEN`` env var
(see ``app/config.py`` — already wired for the external-auth-backend
path in PR-5c). When the token is set AND a request's
``Authorization: Bearer …`` header matches, the middleware returns
immediately without recording the request in any rate-limit window.

Pins
----

1. With ``INTERNAL_AUTH_TOKEN=secret`` and matching header → bypass
   even after many rapid requests (no 429).
2. With ``INTERNAL_AUTH_TOKEN=secret`` and **wrong** header → normal
   per-IP limiting applies; 61st unauthenticated request returns 429.
3. With ``INTERNAL_AUTH_TOKEN`` unset (default) → bypass branch is
   unreachable and behaviour is identical to upstream messaging-v1.0.0.
4. The cue_sk_* per-key limiter still fires when the bypass condition
   isn't met (regression test for the bypass not breaking the existing
   per-key path).
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from app.config import settings


@contextmanager
def _patch_settings(**overrides):
    """Patch settings + restore on exit. Mirrors the helper used in
    test_dock_readiness_packaging_knobs.py."""
    original = {}
    for k, v in overrides.items():
        original[k] = getattr(settings, k)
        setattr(settings, k, v)
    try:
        yield
    finally:
        for k, v in original.items():
            setattr(settings, k, v)


# ─── 1. Matching token bypasses — even at burst rates ─────────────


@pytest.mark.asyncio
async def test_matching_internal_auth_token_bypasses_rate_limit(
    client, redis_client
):
    """With the bypass token set and a matching Authorization header,
    100 rapid requests in the same minute all return 200 (or whatever
    the endpoint normally returns) without ever hitting 429.

    The endpoint we hit (/health) is also in EXEMPT_PATHS, so we use
    /v1/cues which is normally rate-limited. Without the bypass, the
    61st request would 429 even when the token is wrong, so 100 OK
    here is meaningful.
    """
    with _patch_settings(INTERNAL_AUTH_TOKEN="bypass-secret-token-32-chars-min"):
        headers = {"Authorization": "Bearer bypass-secret-token-32-chars-min"}
        for i in range(100):
            resp = await client.get("/v1/cues", headers=headers)
            # 401 (no api_key) is fine; the point is NOT 429.
            assert resp.status_code != 429, (
                f"Request {i + 1}/100 hit rate limit despite matching "
                f"INTERNAL_AUTH_TOKEN: status={resp.status_code}"
            )


# ─── 2. Wrong token → normal limiting kicks in ────────────────────


@pytest.mark.asyncio
async def test_wrong_internal_auth_token_does_not_bypass(client, redis_client):
    """If the bypass token is set but the request's Authorization
    header doesn't match, the request falls through to the normal
    per-IP / per-key limiter. With the default 60/min IP bucket and
    unauthenticated requests, the 61st request returns 429."""
    with _patch_settings(INTERNAL_AUTH_TOKEN="bypass-secret-token-32-chars-min"):
        headers = {"Authorization": "Bearer wrong-token-not-matching"}
        # First 60 unauthenticated requests should pass (per-IP bucket).
        for i in range(60):
            resp = await client.get("/v1/cues", headers=headers)
            # Auth fails (401) but the rate-limit middleware doesn't
            # see auth — it sees the IP and counts.
            assert resp.status_code != 429, f"Request {i + 1}/60 unexpectedly limited"

        # 61st should get 429 from the per-IP bucket.
        resp = await client.get("/v1/cues", headers=headers)
        assert resp.status_code == 429, (
            f"61st request should hit per-IP rate limit (wrong bypass token); "
            f"got {resp.status_code}. Bypass should only fire on EXACT match."
        )


# ─── 3. Token unset → bypass is dead code, behaviour unchanged ────


@pytest.mark.asyncio
async def test_unset_internal_auth_token_keeps_default_behaviour(
    client, redis_client
):
    """With ``INTERNAL_AUTH_TOKEN`` unset (default), no header value
    triggers a bypass — including the empty string. This pins that
    operators who don't opt in see identical behaviour to upstream
    messaging-v1.0.0 (i.e. setting ``Authorization: Bearer ""`` doesn't
    accidentally bypass via comparing-empty-to-empty)."""
    with _patch_settings(INTERNAL_AUTH_TOKEN=""):
        # 61 unauthenticated requests should still trip the per-IP cap.
        for i in range(60):
            await client.get("/v1/cues")
        resp = await client.get("/v1/cues")
        assert resp.status_code == 429, (
            f"With INTERNAL_AUTH_TOKEN unset, default IP-based rate "
            f"limiting must still fire. Got {resp.status_code}."
        )


@pytest.mark.asyncio
async def test_unset_token_does_not_bypass_even_with_empty_bearer(
    client, redis_client
):
    """Defense-in-depth: ``Authorization: Bearer `` (empty token) when
    ``INTERNAL_AUTH_TOKEN`` is also empty must NOT match. The check
    short-circuits on `if internal_token:` so the compare never runs
    with both sides empty."""
    with _patch_settings(INTERNAL_AUTH_TOKEN=""):
        headers = {"Authorization": "Bearer "}
        for i in range(60):
            await client.get("/v1/cues", headers=headers)
        resp = await client.get("/v1/cues", headers=headers)
        assert resp.status_code == 429, (
            "Empty token in the env var must not enable bypass via empty header."
        )


# ─── 4. cue_sk_* per-key limiter still fires when bypass misses ───


@pytest.mark.asyncio
async def test_cue_sk_per_key_limiter_unaffected_by_bypass(
    client, auth_headers, redis_client
):
    """Regression test: the existing cue_sk_* per-key rate limiter
    must continue to work as before. The bypass only triggers on an
    exact token match; cue_sk_* keys never match (they have a
    different prefix), so this test validates that the per-key path
    (60/min for free tier) still trips a 429."""
    with _patch_settings(INTERNAL_AUTH_TOKEN="bypass-secret-token-32-chars-min"):
        # auth_headers carries a cue_sk_* key. The bypass branch
        # compares the full Bearer string — cue_sk_* won't match the
        # bypass token, so the request falls through to per-key
        # limiting.
        for i in range(60):
            resp = await client.get("/v1/cues", headers=auth_headers)
            assert resp.status_code == 200, (
                f"cue_sk_* request {i + 1}/60 unexpectedly limited: "
                f"status={resp.status_code}"
            )

        resp = await client.get("/v1/cues", headers=auth_headers)
        assert resp.status_code == 429, (
            "cue_sk_* per-key limiter should still fire on the 61st "
            "request when the bypass condition isn't met."
        )
