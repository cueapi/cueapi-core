"""Cue payload routing conventions.

Ported from private cueapi (cueapi.ai hosted) ``app/utils/routing.py``
as part of CWS-2026-05-08 Tier 2. Pure helper; no DB / network /
business-logic dependencies. Lives in OSS so the (forthcoming)
``cueapi-presence-runtime`` library can reference the canonical
``DELIVERY_ROUTE_VALUES`` enum without consumers having to vendor it
from private.

Two server-side conventions live here:

1. ``apply_routing_implications`` — when a cue payload sets
   ``routing="live"`` but doesn't set ``live_fallback_mode``, fill in
   ``live_fallback_mode="none"``. The default (no implication) silently
   falls back to a fresh ``claude --print`` BG subprocess if the live
   session doesn't claim within the worker's claim window. Senders that
   want explicit BG fallback still set ``live_fallback_mode="background"``
   themselves; this implication only fires when the field is unset.

2. ``DELIVERY_ROUTE_VALUES`` + ``is_valid_delivery_route`` — canonical
   enum surfaced through ``OutcomeRequest.delivery_route`` (when the
   outcome surface is wired up in OSS) and persisted into
   ``outcome.metadata['delivery_route']`` so senders have a typed
   signal of where the cue actually landed (live session vs
   timeout-fallback BG vs initial-BG).

OSS port note: this helper is wired into cue paths in private cueapi
(``cue_service.create_cue`` / ``update_cue`` / ``effective_payload``);
OSS doesn't yet have those wired. Subsequent PRs may grow the wiring
when the cue/outcome surfaces are extended; the helper landing here
first means later wiring can reference the canonical enum without
churn.
"""

from __future__ import annotations

from typing import Optional

DELIVERY_ROUTE_LIVE = "live"
DELIVERY_ROUTE_BACKGROUND_FALLBACK = "background_fallback"
DELIVERY_ROUTE_BACKGROUND_DIRECT = "background_direct"
# v0.2.x — Live-Monitor claimed the cue but the cmotigtnx attestation
# POST to /v1/executions/{id}/live-claim failed (network blip / timeout
# / server unavailable). Reported via outcome metadata so consumers can
# distinguish "Monitor did claim, just couldn't attest" from a true
# BG-fallback (which would be ``background_fallback``). Cross-codebase
# paired with private cueapi's enum (CWS-2026-05-08 Item 6 lock).
DELIVERY_ROUTE_LIVE_UNVERIFIED = "live_unverified"

DELIVERY_ROUTE_VALUES = frozenset(
    {
        DELIVERY_ROUTE_LIVE,
        DELIVERY_ROUTE_LIVE_UNVERIFIED,
        DELIVERY_ROUTE_BACKGROUND_FALLBACK,
        DELIVERY_ROUTE_BACKGROUND_DIRECT,
    }
)


def is_valid_delivery_route(value: Optional[str]) -> bool:
    return value in DELIVERY_ROUTE_VALUES


def apply_routing_implications(payload: Optional[dict]) -> Optional[dict]:
    """Return ``payload`` with routing implications applied.

    When ``routing == "live"`` and ``live_fallback_mode`` is unset,
    set ``live_fallback_mode = "none"``. Idempotent. Returns the same
    object identity when no change is needed; otherwise a shallow copy.
    Non-dict / None / non-live inputs pass through unchanged.
    """
    if not isinstance(payload, dict):
        return payload
    if payload.get("routing") != "live":
        return payload
    if "live_fallback_mode" in payload:
        return payload
    out = dict(payload)
    out["live_fallback_mode"] = "none"
    return out
