from __future__ import annotations

import json as _json
from starlette.types import ASGIApp, Receive, Scope, Send

MAX_BODY_SIZE = 1 * 1024 * 1024  # 1MB


class BodySizeLimitMiddleware:
    """Raw ASGI middleware that rejects oversized requests before body is read.

    Checks Content-Length header and returns 413 immediately if over limit.
    Also tracks bytes received during body streaming to catch chunked transfers.

    Using raw ASGI (not BaseHTTPMiddleware) avoids Starlette buffering the
    entire body before our check runs, which caused 503 crashes on large payloads.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Check Content-Length header first (fast path)
        headers = dict(scope.get("headers", []))
        content_length_raw = headers.get(b"content-length")
        if content_length_raw is not None:
            try:
                content_length = int(content_length_raw)
            except (ValueError, TypeError):
                content_length = 0
            if content_length > MAX_BODY_SIZE:
                await _send_413(send)
                return

        # Wrap receive to enforce limit on streamed/chunked bodies
        bytes_received = 0

        async def limited_receive() -> dict:
            nonlocal bytes_received
            message = await receive()
            if message.get("type") == "http.request":
                body = message.get("body", b"")
                bytes_received += len(body)
                if bytes_received > MAX_BODY_SIZE:
                    raise _BodyTooLargeError()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLargeError:
            await _send_413(send)


class _BodyTooLargeError(Exception):
    pass


_RESPONSE_BODY = _json.dumps({
    "error": {
        "code": "request_too_large",
        "message": f"Request body exceeds {MAX_BODY_SIZE} bytes",
        "status": 413,
    }
}).encode()


async def _send_413(send: Send) -> None:
    await send({
        "type": "http.response.start",
        "status": 413,
        "headers": [
            [b"content-type", b"application/json"],
            [b"content-length", str(len(_RESPONSE_BODY)).encode()],
        ],
    })
    await send({
        "type": "http.response.body",
        "body": _RESPONSE_BODY,
    })
