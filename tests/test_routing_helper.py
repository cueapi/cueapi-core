"""Tests for the routing helper ported from private cueapi.

Pure unit tests; no DB / HTTP / async fixtures needed. Tests cover:

1. DELIVERY_ROUTE_VALUES enum membership
2. is_valid_delivery_route on valid + invalid inputs
3. apply_routing_implications on the four payload shapes:
   - non-dict (None, "", int) → passthrough
   - non-live routing → passthrough
   - live + live_fallback_mode set → passthrough (don't override)
   - live + live_fallback_mode unset → fills in "none"
4. Idempotency: applying twice equals applying once
5. Returns same object identity when no change needed (memory hint)
"""
from __future__ import annotations

from app.utils.routing import (
    DELIVERY_ROUTE_BACKGROUND_DIRECT,
    DELIVERY_ROUTE_BACKGROUND_FALLBACK,
    DELIVERY_ROUTE_LIVE,
    DELIVERY_ROUTE_VALUES,
    apply_routing_implications,
    is_valid_delivery_route,
)


# ─── DELIVERY_ROUTE_VALUES enum ────────────────────────────────────


def test_delivery_route_values_contains_three_canonical_strings():
    assert DELIVERY_ROUTE_VALUES == frozenset(
        {"live", "background_fallback", "background_direct"}
    )


def test_delivery_route_constants_match_enum_values():
    assert DELIVERY_ROUTE_LIVE == "live"
    assert DELIVERY_ROUTE_BACKGROUND_FALLBACK == "background_fallback"
    assert DELIVERY_ROUTE_BACKGROUND_DIRECT == "background_direct"
    for c in (
        DELIVERY_ROUTE_LIVE,
        DELIVERY_ROUTE_BACKGROUND_FALLBACK,
        DELIVERY_ROUTE_BACKGROUND_DIRECT,
    ):
        assert c in DELIVERY_ROUTE_VALUES


# ─── is_valid_delivery_route ───────────────────────────────────────


def test_is_valid_delivery_route_accepts_canonical_values():
    assert is_valid_delivery_route("live") is True
    assert is_valid_delivery_route("background_fallback") is True
    assert is_valid_delivery_route("background_direct") is True


def test_is_valid_delivery_route_rejects_unknown_values():
    assert is_valid_delivery_route("unknown") is False
    assert is_valid_delivery_route("LIVE") is False  # case-sensitive
    assert is_valid_delivery_route("") is False
    assert is_valid_delivery_route(None) is False


# ─── apply_routing_implications: passthrough cases ────────────────


def test_apply_routing_implications_passes_through_none():
    assert apply_routing_implications(None) is None


def test_apply_routing_implications_passes_through_non_dict():
    """Strings / ints / lists pass through identity."""
    assert apply_routing_implications("not a dict") == "not a dict"
    assert apply_routing_implications(42) == 42
    lst = [1, 2, 3]
    assert apply_routing_implications(lst) is lst


def test_apply_routing_implications_passes_through_non_live_routing():
    """routing=bg payloads are not touched."""
    payload = {"routing": "bg", "task": "max-claude-code-cueapi"}
    result = apply_routing_implications(payload)
    assert result is payload  # same identity, no change
    assert "live_fallback_mode" not in result


def test_apply_routing_implications_passes_through_when_fallback_already_set():
    """Sender explicitly set live_fallback_mode — don't override."""
    payload = {"routing": "live", "live_fallback_mode": "background"}
    result = apply_routing_implications(payload)
    assert result is payload
    assert result["live_fallback_mode"] == "background"


# ─── apply_routing_implications: implication fires ────────────────


def test_apply_routing_implications_fills_in_none_for_unset_fallback():
    """live + unset → live_fallback_mode='none' (the load-bearing
    implication that prevents silent BG-fallback)."""
    payload = {"routing": "live", "task": "max-claude-code-cueapi"}
    result = apply_routing_implications(payload)
    assert result is not payload  # shallow copy returned
    assert result["routing"] == "live"
    assert result["live_fallback_mode"] == "none"
    # Original untouched
    assert "live_fallback_mode" not in payload


# ─── Idempotency ───────────────────────────────────────────────────


def test_apply_routing_implications_idempotent_on_already_applied():
    """Applying the implication twice equals once."""
    payload = {"routing": "live"}
    once = apply_routing_implications(payload)
    twice = apply_routing_implications(once)
    # The second pass sees live_fallback_mode already set, so it
    # passes through identity.
    assert twice is once
    assert twice["live_fallback_mode"] == "none"
