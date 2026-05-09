"""Pluggable authorization backend for cross-user messaging (PR-5b).

Spec: https://trydock.ai/dock/prd/cueapi-port §"PR-5b Pluggable
cross-user authorization".

The default is ``SameTenantAuthorizationBackend`` — it enforces v1 spec
§3.4 (sender and recipient must share user_id). Self-host integrators
who need cross-user messaging within their own permission model
(Dock's case: agents owned by users sharing a workspace can message
each other) override the backend via env var.

Two override modes:

1. ``AUTHORIZATION_BACKEND`` — Python import path to a class that
   subclasses ``AuthorizationBackend``. Loaded at module import time.
   Pattern mirrors the existing ``alert_webhook.py`` plugin convention.
   Use this when you want full control + can ship Python code in
   the cueapi-core deployment.

2. ``AUTHZ_HOOK_URL`` — HTTPS URL the substrate POSTs to before
   accepting any message. Convenient when the integrator's authz
   logic lives in a separate service. Substrate signs the request
   with ``AUTHZ_HOOK_SECRET`` (HMAC-SHA256). Decision can be cached
   for ``cache_ttl`` seconds in Redis to avoid hammering on every
   message.

If both are set, ``AUTHORIZATION_BACKEND`` wins (more direct).

Wire format for the webhook backend:

    POST {AUTHZ_HOOK_URL}
    X-CueAPI-Signature: v1=<hex>
    X-CueAPI-Timestamp: <unix>
    Content-Type: application/json
    {
      "sender_user_id": "<uuid>",
      "recipient_user_id": "<uuid>",
      "sender_agent_id": "agt_...",
      "recipient_agent_id": "agt_...",
      "message_kind": "message",
      "idempotency_key": "<key-or-null>"
    }

Expected response:

    200 OK + {"decision": "allow", "cache_ttl": 60}
    200 OK + {"decision": "deny", "reason": "no shared workspace", "cache_ttl": 60}
    other → fail-closed (deny + log)

The integrator's hook MUST respond within 5 seconds. Timeout = deny.
"""
from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import logging
import time
from abc import ABC, abstractmethod
from typing import Iterable, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class AuthorizationBackend(ABC):
    """Abstract authorization backend.

    Implementers decide whether a sender agent is allowed to message a
    recipient agent. Decision is binary — allow / deny — and any
    contextual reasoning belongs in the integrator's logs, not in the
    return value.
    """

    @abstractmethod
    async def authorize_message(
        self,
        *,
        sender_user_id: str,
        recipient_user_id: str,
        sender_agent_id: str,
        recipient_agent_id: str,
        message_kind: str = "message",
        idempotency_key: Optional[str] = None,
    ) -> bool:
        """Return True if the message should be accepted, False otherwise."""
        ...


class SameTenantAuthorizationBackend(AuthorizationBackend):
    """Default backend — enforces spec §3.4 (same-tenant only).

    Hosted cueapi.ai uses this. Self-hosters override via env var
    when they want cross-user messaging within their own permission
    model (e.g., Dock's workspace-membership rule).
    """

    async def authorize_message(
        self,
        *,
        sender_user_id: str,
        recipient_user_id: str,
        sender_agent_id: str,
        recipient_agent_id: str,
        message_kind: str = "message",
        idempotency_key: Optional[str] = None,
    ) -> bool:
        return str(sender_user_id) == str(recipient_user_id)


class EveryoneAuthorizationBackend(AuthorizationBackend):
    """Reference backend that allows every authenticated send.

    Use only in trusted single-tenant environments where authentication
    itself is the access control. Authentication being correct is
    necessary but not sufficient — without authorization, any compromised
    key can message any agent. **Do not use on a public deployment.**

    Importable: ``from app.services.authorization_backend import EveryoneAuthorizationBackend``.
    Activate via ``AUTHORIZATION_BACKEND=app.services.authorization_backend:EveryoneAuthorizationBackend``.
    """

    async def authorize_message(
        self,
        *,
        sender_user_id: str,
        recipient_user_id: str,
        sender_agent_id: str,
        recipient_agent_id: str,
        message_kind: str = "message",
        idempotency_key: Optional[str] = None,
    ) -> bool:
        return True


class AllowlistAuthorizationBackend(AuthorizationBackend):
    """Reference backend that authorizes a static (sender_user, recipient_user) allowlist.

    Same-user sends are always allowed. Cross-user sends are allowed only
    when the directed pair ``(sender_user_id, recipient_user_id)`` appears
    in the configured allowlist.

    Allowlist is parsed from ``AUTHZ_ALLOWLIST`` env var when constructed
    with no arguments (the resolution path used by the
    ``AUTHORIZATION_BACKEND`` env var). Format is comma-separated directed
    pairs, each pair joined with ``:``::

        AUTHZ_ALLOWLIST=user-a-uuid:user-b-uuid,user-c-uuid:user-d-uuid

    Pairs are directional. To allow bidirectional messaging between A and
    B, configure both ``A:B`` and ``B:A``.

    Self-hosters that prefer in-process Python configuration can subclass
    or instantiate directly with ``allowed_pairs``::

        from app.services.authorization_backend import AllowlistAuthorizationBackend

        backend = AllowlistAuthorizationBackend(
            allowed_pairs={("user-a-uuid", "user-b-uuid")},
        )

    Suitable for small, slow-changing pair-wise relationships
    (system-to-system bridges, controlled multi-tenant pilots).

    Importable: ``from app.services.authorization_backend import AllowlistAuthorizationBackend``.
    Activate via ``AUTHORIZATION_BACKEND=app.services.authorization_backend:AllowlistAuthorizationBackend``
    plus ``AUTHZ_ALLOWLIST=...`` for the env-driven config path.
    """

    def __init__(self, allowed_pairs: Optional["Iterable[tuple[str, str]]"] = None):
        if allowed_pairs is None:
            allowed_pairs = self._parse_env_allowlist(settings.AUTHZ_ALLOWLIST)
        self._allowed: frozenset[tuple[str, str]] = frozenset(
            (str(s), str(r)) for s, r in allowed_pairs
        )

    @staticmethod
    def _parse_env_allowlist(raw: str) -> "list[tuple[str, str]]":
        """Parse ``AUTHZ_ALLOWLIST`` env var into a list of directed pairs.

        Format: ``"sender:recipient,sender:recipient,..."``. Whitespace
        around tokens is stripped. Empty string returns an empty list.
        Malformed entries (missing colon, empty halves) are silently
        dropped with a warning log so a typo can't lock out the
        substrate by raising at module import time.
        """
        if not raw:
            return []
        pairs: list[tuple[str, str]] = []
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            if ":" not in entry:
                logger.warning(
                    "AUTHZ_ALLOWLIST entry missing ':' separator; skipping",
                    extra={"event_type": "authz_allowlist_malformed_entry", "entry_len": len(entry)},
                )
                continue
            sender, _, recipient = entry.partition(":")
            sender = sender.strip()
            recipient = recipient.strip()
            if not sender or not recipient:
                logger.warning(
                    "AUTHZ_ALLOWLIST entry has empty half; skipping",
                    extra={"event_type": "authz_allowlist_empty_half"},
                )
                continue
            pairs.append((sender, recipient))
        return pairs

    async def authorize_message(
        self,
        *,
        sender_user_id: str,
        recipient_user_id: str,
        sender_agent_id: str,
        recipient_agent_id: str,
        message_kind: str = "message",
        idempotency_key: Optional[str] = None,
    ) -> bool:
        if str(sender_user_id) == str(recipient_user_id):
            return True
        return (str(sender_user_id), str(recipient_user_id)) in self._allowed


class WebhookAuthorizationBackend(AuthorizationBackend):
    """Calls an integrator-provided HTTPS hook for each authz decision.

    The hook receives a signed JSON payload and returns
    {"decision": "allow"|"deny", "cache_ttl": <seconds>, "reason": <str>?}.
    Substrate caches the decision in Redis keyed on
    ``(sender_user_id, recipient_user_id, message_kind)`` for
    ``cache_ttl`` seconds (default 60) to avoid hammering on every
    message in a hot conversation.

    Fail-closed semantics: any non-2xx response, timeout, or invalid
    response body is treated as a deny decision. The hook is
    security-critical and MUST be reliable; flaky hooks block legit
    traffic.
    """

    DEFAULT_TIMEOUT_SECONDS = 5
    DEFAULT_CACHE_TTL_SECONDS = 60

    def __init__(
        self,
        hook_url: str,
        hook_secret: str = "",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.hook_url = hook_url
        self.hook_secret = hook_secret
        self.timeout_seconds = timeout_seconds

    async def authorize_message(
        self,
        *,
        sender_user_id: str,
        recipient_user_id: str,
        sender_agent_id: str,
        recipient_agent_id: str,
        message_kind: str = "message",
        idempotency_key: Optional[str] = None,
    ) -> bool:
        # Cache short-circuit: same (sender_user, recipient_user, kind)
        # within cache_ttl returns the cached decision. Fail-open on
        # Redis blip — the next allow/deny POST will refresh the
        # cache.
        from app.redis import get_redis  # late import to avoid cycle
        cache_key = f"authz:{sender_user_id}:{recipient_user_id}:{message_kind}"
        try:
            redis = await get_redis()
            cached = await redis.get(cache_key)
            if cached is not None:
                return cached == "1"
        except Exception:
            logger.warning("Redis unavailable for authz cache; calling hook")

        # Make the actual HTTP call.
        body = {
            "sender_user_id": str(sender_user_id),
            "recipient_user_id": str(recipient_user_id),
            "sender_agent_id": sender_agent_id,
            "recipient_agent_id": recipient_agent_id,
            "message_kind": message_kind,
            "idempotency_key": idempotency_key,
        }
        body_bytes = json.dumps(body, sort_keys=True).encode("utf-8")
        timestamp = str(int(time.time()))
        signature = ""
        if self.hook_secret:
            mac = hmac.new(
                self.hook_secret.encode("utf-8"),
                f"{timestamp}.".encode("utf-8") + body_bytes,
                hashlib.sha256,
            )
            signature = f"v1={mac.hexdigest()}"

        headers = {
            "Content-Type": "application/json",
            "X-CueAPI-Timestamp": timestamp,
        }
        if signature:
            headers["X-CueAPI-Signature"] = signature

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.post(self.hook_url, content=body_bytes, headers=headers)
        except (httpx.TimeoutException, httpx.RequestError) as e:
            logger.warning(
                "Authz hook unreachable; denying",
                extra={
                    "event_type": "authz_hook_unreachable",
                    "error": str(e),
                    "hook_url_host": _safe_host(self.hook_url),
                },
            )
            return False

        if resp.status_code != 200:
            logger.warning(
                "Authz hook non-200; denying",
                extra={
                    "event_type": "authz_hook_non_200",
                    "status": resp.status_code,
                    "hook_url_host": _safe_host(self.hook_url),
                },
            )
            return False

        try:
            payload = resp.json()
        except (ValueError, TypeError):
            logger.warning("Authz hook returned non-JSON; denying")
            return False

        decision = payload.get("decision")
        if decision not in ("allow", "deny"):
            logger.warning(
                "Authz hook returned invalid decision",
                extra={"event_type": "authz_hook_invalid_decision", "decision": decision},
            )
            return False

        allow = decision == "allow"
        cache_ttl = int(payload.get("cache_ttl", self.DEFAULT_CACHE_TTL_SECONDS))
        if cache_ttl > 0:
            try:
                redis = await get_redis()
                await redis.set(cache_key, "1" if allow else "0", ex=cache_ttl)
            except Exception:
                pass  # cache miss on next call is fine

        if not allow:
            logger.info(
                "Authz hook denied",
                extra={
                    "event_type": "authz_hook_denied",
                    "reason": payload.get("reason", ""),
                    "sender_user_id": str(sender_user_id),
                    "recipient_user_id": str(recipient_user_id),
                },
            )
        return allow


def _safe_host(url: str) -> str:
    """Extract host without leaking full URL in logs."""
    try:
        from urllib.parse import urlparse
        return urlparse(url).hostname or "?"
    except Exception:
        return "?"


# ─── Backend resolution ────────────────────────────────────────────


_cached_backend: Optional[AuthorizationBackend] = None


def get_authorization_backend() -> AuthorizationBackend:
    """Return the configured backend instance.

    Resolution order (first hit wins):

    1. ``AUTHORIZATION_BACKEND`` env var — Python import path. Imported
       once on first call, then cached.
    2. ``AUTHZ_HOOK_URL`` env var — instantiates ``WebhookAuthorizationBackend``.
    3. Default: ``SameTenantAuthorizationBackend``.

    Cached at module level. Restart required to pick up new config.
    """
    global _cached_backend
    if _cached_backend is not None:
        return _cached_backend

    # 1. Custom backend by import path.
    if settings.AUTHORIZATION_BACKEND:
        path = settings.AUTHORIZATION_BACKEND
        # Format: "package.module:ClassName"
        try:
            module_path, class_name = path.rsplit(":", 1)
            module = importlib.import_module(module_path)
            cls = getattr(module, class_name)
            instance = cls()
            if not isinstance(instance, AuthorizationBackend):
                raise TypeError(
                    f"{path} does not subclass AuthorizationBackend"
                )
            _cached_backend = instance
            logger.info(
                "Loaded authorization backend from AUTHORIZATION_BACKEND env var",
                extra={"event_type": "authz_backend_loaded", "path": path},
            )
            return _cached_backend
        except Exception as e:
            logger.error(
                "Failed to load AUTHORIZATION_BACKEND; falling back",
                extra={"event_type": "authz_backend_load_failed", "path": path, "error": str(e)},
            )

    # 2. Webhook backend by URL.
    if settings.AUTHZ_HOOK_URL:
        _cached_backend = WebhookAuthorizationBackend(
            hook_url=settings.AUTHZ_HOOK_URL,
            hook_secret=settings.AUTHZ_HOOK_SECRET,
        )
        logger.info(
            "Loaded WebhookAuthorizationBackend from AUTHZ_HOOK_URL",
            extra={"event_type": "authz_backend_loaded", "type": "webhook"},
        )
        return _cached_backend

    # 3. Default.
    _cached_backend = SameTenantAuthorizationBackend()
    return _cached_backend


def _reset_cached_backend_for_tests() -> None:
    """Test-only: clear the module-level cache so settings overrides
    can re-resolve. Production code never calls this."""
    global _cached_backend
    _cached_backend = None
