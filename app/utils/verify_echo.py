"""Body-verify echo-back primitive (Layer 1 of silent-body-corruption defense).

When request header ``X-CueAPI-Verify-Echo: true`` is present on a supported
endpoint, the server adds ``body_received`` and ``body_received_sha256``
fields to the 200 response. The caller diffs sent body vs received to detect
caller-side shell expansion (backticks, $-paren, ${VAR}) that silently
corrupts body content before send-time.

Why this exists: 2026-05-11 ~22:00Z, CMA's outbound Cue Messages 0/6 to
cue-pm fell to garbage via inline bash ``-d "$BODY"`` where $BODY had been
mutated by shell expansion at variable-assignment time. Server received
valid JSON with wrong content; no layer fails loud. Echo-back is the
keystone for the 4-layer defense: substrate (this), SDK auto-verify, CLI
force-file mode, docs leading with file-payload pattern.

Design doc: https://trydock.ai/workspaces/cue-message-silent-corruption-substrate-design-2026-05-11
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Optional

from fastapi import Request


VERIFY_ECHO_HEADER = "X-CueAPI-Verify-Echo"


def verify_echo_requested(request: Request) -> bool:
    """True iff ``X-CueAPI-Verify-Echo: true`` header is present (case-insensitive)."""
    return request.headers.get(VERIFY_ECHO_HEADER, "").strip().lower() == "true"


def _canonical_json_bytes(value: Any) -> bytes:
    """Stable JSON serialization for hashing: sorted keys + no whitespace."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def apply_verify_echo(*, request: Request, parsed_body: Optional[Any]) -> Dict[str, Any]:
    """Return verify-echo fields to merge into the response.

    Returns ``{}`` when the header is absent (zero-cost no-op for non-opted
    clients). When present, returns::

        {
            "body_received": <parsed body dict / str / None>,
            "body_received_sha256": <64-char hex digest>,
        }

    Hashing rule:

    * ``None`` body → SHA256 of empty bytes (well-known constant).
    * Pydantic model → ``model_dump(mode="json")`` then canonical JSON.
    * dict → canonical JSON.
    * Otherwise → ``str()`` then UTF-8 bytes.

    The dict is intended to be ``.update()``-merged into the response dict
    or returned alongside other fields. Caller is responsible for placement.
    """
    if not verify_echo_requested(request):
        return {}

    if parsed_body is None:
        body_view: Any = None
        sha_input: bytes = b""
    elif hasattr(parsed_body, "model_dump"):
        body_view = parsed_body.model_dump(mode="json")
        sha_input = _canonical_json_bytes(body_view)
    elif isinstance(parsed_body, dict):
        body_view = parsed_body
        sha_input = _canonical_json_bytes(body_view)
    else:
        body_view = str(parsed_body)
        sha_input = body_view.encode("utf-8")

    return {
        "body_received": body_view,
        "body_received_sha256": hashlib.sha256(sha_input).hexdigest(),
    }
