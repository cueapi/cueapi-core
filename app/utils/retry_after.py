"""Parse the HTTP ``Retry-After`` header per RFC 7231 §7.1.3.

Used by the messaging push-delivery path (Phase 12.1.5 Slice 3) to
honor recipient-supplied retry windows on 429 / 503 responses while
preserving the server's polite minimum backoff.

v1.5 supports the **seconds-as-int** form only. The HTTP-date form
(``Retry-After: Wed, 21 Oct 2026 07:28:00 GMT``) is intentionally
NOT parsed — rare in practice, adds parse complexity for near-zero
real-world benefit. Date-form values are treated as absent (no
Retry-After honoring, fall back to own backoff).
"""
from __future__ import annotations

from typing import Optional


def parse_retry_after(
    header_value: Optional[str],
    *,
    own_min_seconds: int,
) -> int:
    """Resolve the effective backoff from a ``Retry-After`` header.

    Args:
        header_value: Raw header value (``None`` or empty string if
            absent). Whitespace-trimmed before parsing.
        own_min_seconds: Server's own minimum backoff floor for this
            attempt. Always wins for very small or zero
            ``Retry-After`` values; recipient cannot bypass the
            server's polite minimum.

    Returns:
        ``max(own_min_seconds, parsed_seconds)`` when the header
        carries a non-negative integer; ``own_min_seconds`` otherwise
        (header absent, malformed, negative, or HTTP-date form).

    The ``max(...)`` floor is the design choice: ``Retry-After: 0``
    shouldn't bypass the server's polite minimum; the recipient's
    value is a *floor for their readiness*, not a ceiling on the
    server's backoff.
    """
    if not header_value:
        return own_min_seconds
    candidate = header_value.strip()
    if not candidate:
        return own_min_seconds
    try:
        parsed = int(candidate)
    except ValueError:
        # Non-integer (HTTP-date form, malformed, decimal, etc.).
        # Treat as absent — fall back to own backoff.
        return own_min_seconds
    if parsed < 0:
        return own_min_seconds
    return max(own_min_seconds, parsed)
