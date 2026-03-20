from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Tuple


def sign_payload(payload: dict, secret: str) -> Tuple[str, str]:
    """Sign a webhook payload with timestamp (Stripe-style).

    Returns (timestamp, signature) where:
    - timestamp: Unix epoch string
    - signature: v1={hex_digest} of "timestamp.payload"

    The signed message is "{timestamp}.{json_payload}" to bind the
    timestamp to the payload and prevent replay attacks.
    """
    timestamp = str(int(time.time()))
    payload_bytes = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    signed_content = f"{timestamp}.".encode("utf-8") + payload_bytes
    signature = hmac.new(
        secret.encode("utf-8"), signed_content, hashlib.sha256
    ).hexdigest()
    return timestamp, f"v1={signature}"


def verify_signature(
    payload: dict,
    secret: str,
    timestamp: str,
    signature: str,
    tolerance_seconds: int = 300,
) -> bool:
    """Verify a timestamped webhook signature.

    Args:
        payload: The webhook body dict
        secret: The user's webhook_secret
        timestamp: The X-CueAPI-Timestamp header value
        signature: The X-CueAPI-Signature header value (v1=...)
        tolerance_seconds: Max age of signature in seconds (default 5 min)

    Returns True if signature is valid and not expired.
    """
    # Check timestamp freshness (replay protection)
    try:
        ts = int(timestamp)
    except (ValueError, TypeError):
        return False

    if abs(time.time() - ts) > tolerance_seconds:
        return False

    # Recompute signature
    payload_bytes = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    signed_content = f"{timestamp}.".encode("utf-8") + payload_bytes
    expected = hmac.new(
        secret.encode("utf-8"), signed_content, hashlib.sha256
    ).hexdigest()
    expected_sig = f"v1={expected}"

    return hmac.compare_digest(expected_sig, signature)
