"""Body-verify echo-back primitive (Layer 1 of silent-body-corruption defense).

When request header ``X-CueAPI-Verify-Echo: true`` is present on a supported
endpoint, the server adds ``body_received`` and ``body_received_sha256``
fields to the 200/201 response. The caller diffs sent body vs received to
detect caller-side shell expansion (backticks, $-paren, ${VAR}) that
silently corrupts body content before send-time.

Why this exists: 2026-05-11 ~22:00Z, CMA's outbound Cue Messages 0/6 to
cue-pm fell to garbage via inline bash ``-d "$BODY"`` where $BODY had been
mutated by shell expansion at variable-assignment time. Server received
valid JSON with wrong content; no layer fails loud. Echo-back is the
keystone for the 4-layer defense: substrate (this), SDK auto-verify, CLI
force-file mode, docs leading with file-payload pattern.

Spec shape (locked at design review, Phase 1 hotfix corrected post-merge):

* ``body_received`` is the **STRING** value of the body field the caller
  sent (e.g. ``MessageCreate.body`` on /v1/messages, ``payload_override.message``
  or similar on /v1/cues/<id>/fire). NOT the full parsed Pydantic envelope dump.
* ``body_received_sha256`` is the SHA256 hex digest of those exact UTF-8 bytes
  so a caller can compute ``sha256(sent_body_bytes).hexdigest()`` locally and
  compare directly. Hash-of-the-string == hash-of-the-bytes.

Design doc: https://trydock.ai/workspaces/cue-message-silent-corruption-substrate-design-2026-05-11
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, Optional

from fastapi import Request


VERIFY_ECHO_HEADER = "X-CueAPI-Verify-Echo"


def verify_echo_requested(request: Request) -> bool:
    """True iff ``X-CueAPI-Verify-Echo: true`` header is present (case-insensitive)."""
    return request.headers.get(VERIFY_ECHO_HEADER, "").strip().lower() == "true"


def apply_verify_echo(*, request: Request, body_text: Optional[str]) -> Dict[str, Any]:
    """Return verify-echo fields to merge into the response.

    Returns ``{}`` when the header is absent (zero-cost no-op for non-opted
    clients). When present, returns::

        {
            "body_received": <body_text — str or None>,
            "body_received_sha256": <64-char hex digest>,
        }

    Hashing rule:

    * ``None`` body → SHA256 of empty bytes (well-known constant); ``body_received``
      is ``None``.
    * Otherwise → caller passes the EXACT string they want echoed (typically the
      ``body`` field of a message or the user-content field of a fire payload);
      ``body_received`` is that string verbatim and the hash is over its UTF-8
      bytes.

    Caller-side verification recipe (mirrors what cueapi-python's auto-verify
    does)::

        import hashlib
        sent = "..."  # the exact string you POSTed in the body field
        resp = client.post(..., json={"body": sent},
                           headers={"X-CueAPI-Verify-Echo": "true"})
        assert resp.json()["body_received"] == sent
        assert resp.json()["body_received_sha256"] == hashlib.sha256(
            sent.encode("utf-8")
        ).hexdigest()
    """
    if not verify_echo_requested(request):
        return {}

    if body_text is None:
        return {
            "body_received": None,
            "body_received_sha256": hashlib.sha256(b"").hexdigest(),
        }

    return {
        "body_received": body_text,
        "body_received_sha256": hashlib.sha256(body_text.encode("utf-8")).hexdigest(),
    }
