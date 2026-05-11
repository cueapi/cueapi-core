"""Universal BodyVerify Layer 1.5 middleware.

Phase 1 wired echo-back per-handler on POST /v1/messages + POST /v1/cues/<id>/fire.
This middleware extends the same primitive to EVERY POST/PATCH/PUT endpoint with
a JSON body, so the substrate-keystone protection isn't endpoint-by-endpoint.

Design choice: raw ASGI middleware (not BaseHTTPMiddleware) so we can capture
both the inbound request body bytes and the outbound response stream without
fighting Starlette's buffering. Same shape as ``BodySizeLimitMiddleware``.

Behavior:

* No-op unless the request carries ``X-CueAPI-Verify-Echo: true`` (case-
  insensitive, whitespace-stripped). Header absent → zero perf cost path.
* Captures raw request body bytes BEFORE the handler runs; re-emits them via
  the ``receive`` callable so handlers can read the body normally.
* Captures response body bytes AFTER the handler returns; if the response is
  application/json + dict-shaped + status 2xx, injects ``body_received``
  (parsed from raw bytes) + ``body_received_sha256`` (SHA256 over canonical
  JSON of the parsed body, or over the raw bytes for non-JSON payloads).
* Idempotent — skips injection if response already has ``body_received``
  (Phase 1 endpoints preserve their per-handler behavior; this middleware is
  a coverage gap-filler, not a replacement).
* Only acts on POST/PATCH/PUT (methods with bodies).
"""
from __future__ import annotations

import hashlib
import json as _json
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send


VERIFY_ECHO_HEADER = b"x-cueapi-verify-echo"
_BODY_METHODS = {"POST", "PATCH", "PUT"}


def _canonical_json_bytes(value: Any) -> bytes:
    return _json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _verify_echo_requested(headers: list[tuple[bytes, bytes]]) -> bool:
    for k, v in headers:
        if k.lower() == VERIFY_ECHO_HEADER:
            return v.strip().lower() == b"true"
    return False


class VerifyEchoMiddleware:
    """Raw ASGI middleware — universal echo-back primitive."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "").upper()
        if method not in _BODY_METHODS:
            await self.app(scope, receive, send)
            return

        if not _verify_echo_requested(scope.get("headers", [])):
            await self.app(scope, receive, send)
            return

        # Capture the request body. We must replay it via receive so the
        # downstream handler can still read it via `await request.body()`
        # / Pydantic body parsing.
        body_chunks: list[bytes] = []
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] != "http.request":
                # Non-body lifecycle messages (e.g. http.disconnect) — pass
                # through to the app as-is. Practically rare on the body
                # ingest path but handled defensively.
                break
            body_chunks.append(message.get("body", b"") or b"")
            more_body = bool(message.get("more_body", False))
        request_body_bytes = b"".join(body_chunks)

        # Re-emit the captured body to the handler. After the body is fully
        # sent (one chunk + more_body=False), subsequent receive() calls
        # should hang waiting for the next request — typical ASGI pattern
        # for after-body lifecycle messages — but Starlette doesn't poll
        # after parsing, so emitting once is sufficient.
        _replayed = {"sent": False}

        async def receive_replay() -> Message:
            if not _replayed["sent"]:
                _replayed["sent"] = True
                return {
                    "type": "http.request",
                    "body": request_body_bytes,
                    "more_body": False,
                }
            # If asked again (rare), defer to the original receive — for
            # disconnect events etc.
            return await receive()

        # Capture the response. We need to read the streaming body, decide
        # whether to inject, and re-emit a possibly-modified body.
        response_status: dict = {"code": 200}
        response_headers: dict = {"items": []}
        response_chunks: list[bytes] = []

        async def send_capture(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_status["code"] = message.get("status", 200)
                response_headers["items"] = list(message.get("headers", []))
                # Defer sending — we may need to mutate Content-Length.
                return
            if message["type"] == "http.response.body":
                response_chunks.append(message.get("body", b"") or b"")
                if message.get("more_body", False):
                    return
                # Final chunk — process now, then emit.
                modified = self._maybe_inject(
                    status_code=response_status["code"],
                    headers=response_headers["items"],
                    request_body_bytes=request_body_bytes,
                    response_body_bytes=b"".join(response_chunks),
                )
                # Emit start with possibly-mutated headers
                await send(
                    {
                        "type": "http.response.start",
                        "status": response_status["code"],
                        "headers": modified["headers"],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": modified["body"],
                        "more_body": False,
                    }
                )
                return
            # Pass through any other message types
            await send(message)

        await self.app(scope, receive_replay, send_capture)

    @staticmethod
    def _maybe_inject(
        *,
        status_code: int,
        headers: list[tuple[bytes, bytes]],
        request_body_bytes: bytes,
        response_body_bytes: bytes,
    ) -> dict:
        """Decide whether to inject echo fields; return final {headers, body}."""
        # Only inject on 2xx — error responses keep their shape so callers
        # don't get noisy echo fields on validation errors etc.
        if not (200 <= status_code < 300):
            return {"headers": headers, "body": response_body_bytes}

        # Inspect Content-Type — only JSON responses are candidates.
        content_type = b""
        for k, v in headers:
            if k.lower() == b"content-type":
                content_type = v.lower()
                break
        if b"application/json" not in content_type:
            return {"headers": headers, "body": response_body_bytes}

        # Parse the response. Skip if it isn't a JSON object (lists, scalars,
        # etc. don't get echo fields).
        try:
            resp_dict = _json.loads(response_body_bytes)
        except _json.JSONDecodeError:
            return {"headers": headers, "body": response_body_bytes}
        if not isinstance(resp_dict, dict):
            return {"headers": headers, "body": response_body_bytes}

        # Idempotent: respect existing body_received from Phase 1 handlers.
        if "body_received" in resp_dict:
            return {"headers": headers, "body": response_body_bytes}

        # Parse the request body. Fall back to raw bytes if not valid JSON.
        body_view: Any
        sha_input: bytes
        if not request_body_bytes:
            body_view = None
            sha_input = b""
        else:
            try:
                body_view = _json.loads(request_body_bytes)
                sha_input = _canonical_json_bytes(body_view)
            except _json.JSONDecodeError:
                body_view = request_body_bytes.decode("utf-8", errors="replace")
                sha_input = request_body_bytes

        resp_dict["body_received"] = body_view
        resp_dict["body_received_sha256"] = hashlib.sha256(sha_input).hexdigest()

        new_body = _json.dumps(resp_dict).encode("utf-8")

        # Update Content-Length header (others preserved).
        new_headers: list[tuple[bytes, bytes]] = []
        for k, v in headers:
            if k.lower() == b"content-length":
                continue
            new_headers.append((k, v))
        new_headers.append((b"content-length", str(len(new_body)).encode("ascii")))
        return {"headers": new_headers, "body": new_body}
