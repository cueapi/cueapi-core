"""Push-delivery outcome classification.

Spec: <https://trydock.ai/mike/cueapi-messaging-primitive-v1-sp> §5.4.

Classifies the outcome of a single push delivery attempt — HTTP
status code or transport-level exception — into a category
(`success` / `retryable` / `terminal`) plus a granular error type
and structured-log event type.

Used by ``deliver_message_task`` and ``retry_message_task`` (the
latter lands in Slice 3b) to decide whether to mark the message
``delivered`` / schedule a retry / mark ``failed``.

The category-and-error-type taxonomy is the source of truth in
``MESSAGING_SPEC §5.4``. Update both this module and the spec
table together if either changes — a recipient debugging a
``msg_delivery_tls_handshake_failed`` event needs the spec to
explain what triggered it.
"""
from __future__ import annotations

import ssl
from dataclasses import dataclass
from typing import Literal, Optional

import httpx

# ── Outcome categories (spec §5.4 column headers) ──────────────────

Category = Literal["success", "retryable", "terminal"]


# ── Error-type taxonomy (spec §5.4 row labels) ─────────────────────
#
# Each error_type maps 1:1 to a row in the spec classification
# table. Recipients debugging a failure see the error_type in the
# ``failed`` message's ``error_message`` field; the spec row
# explains what triggered it.

# Terminal — recipient explicitly rejected the message.
ERR_AUTH_FAILED = "auth_failed"                  # 401: wrong/rotated webhook_secret
ERR_ENDPOINT_MISSING = "endpoint_missing"        # 404: typo in webhook_url or removed route
ERR_METHOD_NOT_ALLOWED = "method_not_allowed"    # 405: doesn't accept POST
ERR_CLIENT_ERROR = "client_error"                # other 4xx (400, 406, 410, 412, etc.)

# Retryable — transient infrastructure or recipient-side blip.
ERR_REQUEST_TIMEOUT = "request_timeout"          # 408
ERR_RATE_LIMITED = "rate_limited"                # 429 (honor Retry-After)
ERR_BAD_GATEWAY = "bad_gateway"                  # 502 (proxy upstream not responding)
ERR_SERVICE_UNAVAILABLE = "service_unavailable"  # 503 (honor Retry-After)
ERR_SERVER_ERROR = "server_error"                # other 5xx
ERR_TLS_HANDSHAKE_FAILED = "tls_handshake_failed"
ERR_CONNECTION_REFUSED = "connection_refused"
ERR_DNS_RESOLUTION_FAILED = "dns_resolution_failed"
ERR_TIMEOUT = "timeout"                          # httpx TimeoutException
ERR_NETWORK = "network"                          # generic httpx NetworkError
ERR_TRANSPORT = "transport"                      # other httpx TransportError

# Success
ERR_NONE = "none"


# ── Log-event taxonomy (spec §5.4 log table) ───────────────────────

EVT_DELIVERED = "msg_delivered"
EVT_RETRY_SCHEDULED = "msg_delivery_retry_scheduled"
EVT_RETRIES_EXHAUSTED = "msg_delivery_retries_exhausted"
EVT_4XX_TERMINAL = "msg_delivery_4xx_terminal"
EVT_429_RETRY_AFTER = "msg_delivery_429_retry_after_honored"
EVT_5XX = "msg_delivery_5xx"
EVT_TLS_FAIL = "msg_delivery_tls_handshake_failed"
EVT_TIMEOUT = "msg_delivery_timeout"
EVT_CONN_REFUSED = "msg_delivery_connection_refused"
EVT_DNS_FAIL = "msg_delivery_dns_resolution_failed"
EVT_NETWORK = "msg_delivery_network_error"


@dataclass(frozen=True)
class DeliveryClassification:
    """The verdict on a single push-delivery attempt."""

    category: Category
    error_type: str
    log_event_type: str
    error_message: str  # Human-readable, suitable for ``messages.error_message`` storage
    http_status: Optional[int]

    @property
    def is_retryable(self) -> bool:
        return self.category == "retryable"

    @property
    def is_terminal(self) -> bool:
        return self.category == "terminal"

    @property
    def is_success(self) -> bool:
        return self.category == "success"


def classify_response(http_status: int) -> DeliveryClassification:
    """Classify by HTTP status code (no exception raised).

    Used when ``deliver_message_to_webhook`` returned a real
    response from the recipient. Maps to the §5.4 status-code rows.
    """
    if 200 <= http_status < 300:
        return DeliveryClassification(
            category="success",
            error_type=ERR_NONE,
            log_event_type=EVT_DELIVERED,
            error_message="",
            http_status=http_status,
        )

    # 4xx — most are terminal; 408 and 429 are retryable.
    if http_status == 408:
        return DeliveryClassification(
            category="retryable",
            error_type=ERR_REQUEST_TIMEOUT,
            log_event_type=EVT_TIMEOUT,
            error_message="Recipient returned 408 Request Timeout",
            http_status=http_status,
        )
    if http_status == 429:
        return DeliveryClassification(
            category="retryable",
            error_type=ERR_RATE_LIMITED,
            log_event_type=EVT_429_RETRY_AFTER,
            error_message="Recipient returned 429 Too Many Requests",
            http_status=http_status,
        )
    if http_status == 401:
        return DeliveryClassification(
            category="terminal",
            error_type=ERR_AUTH_FAILED,
            log_event_type=EVT_4XX_TERMINAL,
            error_message="Recipient returned 401 Unauthorized — webhook_secret may be wrong or rotated",
            http_status=http_status,
        )
    if http_status == 404:
        return DeliveryClassification(
            category="terminal",
            error_type=ERR_ENDPOINT_MISSING,
            log_event_type=EVT_4XX_TERMINAL,
            error_message="Recipient returned 404 Not Found — webhook_url may be incorrect or the route was removed",
            http_status=http_status,
        )
    if http_status == 405:
        return DeliveryClassification(
            category="terminal",
            error_type=ERR_METHOD_NOT_ALLOWED,
            log_event_type=EVT_4XX_TERMINAL,
            error_message="Recipient returned 405 Method Not Allowed — recipient does not accept POST on this URL",
            http_status=http_status,
        )
    if 400 <= http_status < 500:
        return DeliveryClassification(
            category="terminal",
            error_type=ERR_CLIENT_ERROR,
            log_event_type=EVT_4XX_TERMINAL,
            error_message=f"Recipient rejected the message ({http_status})",
            http_status=http_status,
        )

    # 5xx — all retryable (502/503 explicitly per Max's review).
    if http_status == 502:
        return DeliveryClassification(
            category="retryable",
            error_type=ERR_BAD_GATEWAY,
            log_event_type=EVT_5XX,
            error_message="Recipient returned 502 Bad Gateway — proxy upstream not responding",
            http_status=http_status,
        )
    if http_status == 503:
        return DeliveryClassification(
            category="retryable",
            error_type=ERR_SERVICE_UNAVAILABLE,
            log_event_type=EVT_5XX,
            error_message="Recipient returned 503 Service Unavailable",
            http_status=http_status,
        )
    if 500 <= http_status < 600:
        return DeliveryClassification(
            category="retryable",
            error_type=ERR_SERVER_ERROR,
            log_event_type=EVT_5XX,
            error_message=f"Recipient returned server error ({http_status})",
            http_status=http_status,
        )

    # 1xx / 3xx / weird — treat as terminal client_error (we don't
    # follow redirects per §5.5 SSRF design, and 1xx/3xx aren't
    # valid terminal responses for a POST). Don't retry unknowns.
    return DeliveryClassification(
        category="terminal",
        error_type=ERR_CLIENT_ERROR,
        log_event_type=EVT_4XX_TERMINAL,
        error_message=f"Unexpected response status ({http_status})",
        http_status=http_status,
    )


def classify_exception(exception: BaseException) -> DeliveryClassification:
    """Classify when delivery raised before getting a response.

    Maps httpx transport-level exceptions to retryable categories
    with granular error_type so the spec §5.4 table covers every
    case a recipient might see in logs.

    TLS handshake failure detection: httpx wraps ``ssl.SSLError``
    inside ``httpx.ConnectError``. Inspect ``__cause__`` to
    distinguish from "TCP connect refused" or "DNS lookup failed."

    DNS resolution failure detection: httpx wraps
    ``socket.gaierror`` inside ``httpx.ConnectError``. Same
    ``__cause__`` inspection pattern.
    """
    # Order matters: timeout subclasses TransportError; check first.
    if isinstance(exception, httpx.TimeoutException):
        return DeliveryClassification(
            category="retryable",
            error_type=ERR_TIMEOUT,
            log_event_type=EVT_TIMEOUT,
            error_message=f"Push delivery timed out: {exception}",
            http_status=None,
        )

    if isinstance(exception, httpx.ConnectError):
        cause = getattr(exception, "__cause__", None)
        if isinstance(cause, ssl.SSLError):
            return DeliveryClassification(
                category="retryable",
                error_type=ERR_TLS_HANDSHAKE_FAILED,
                log_event_type=EVT_TLS_FAIL,
                error_message=(
                    f"TLS handshake failed: {cause}. "
                    "Often transient (cert renewal, intermediate cert propagation); "
                    "retried automatically."
                ),
                http_status=None,
            )
        # socket.gaierror => DNS resolution failure
        if cause is not None and type(cause).__name__ == "gaierror":
            return DeliveryClassification(
                category="retryable",
                error_type=ERR_DNS_RESOLUTION_FAILED,
                log_event_type=EVT_DNS_FAIL,
                error_message=f"DNS resolution failed for webhook host: {cause}",
                http_status=None,
            )
        # Generic connect error: connection refused or unreachable.
        return DeliveryClassification(
            category="retryable",
            error_type=ERR_CONNECTION_REFUSED,
            log_event_type=EVT_CONN_REFUSED,
            error_message=f"Connection refused or unreachable: {exception}",
            http_status=None,
        )

    if isinstance(exception, httpx.NetworkError):
        # ReadError / WriteError / CloseError — connection broke
        # mid-stream. Retryable; transient by nature.
        return DeliveryClassification(
            category="retryable",
            error_type=ERR_NETWORK,
            log_event_type=EVT_NETWORK,
            error_message=f"Network error during delivery: {exception}",
            http_status=None,
        )

    if isinstance(exception, httpx.TransportError):
        # ProxyError, UnsupportedProtocol, ProtocolError — covers
        # the rare transport-level failures we don't have a
        # dedicated bucket for. Retry; if it's deterministic the
        # retries will hit terminal exhaustion.
        return DeliveryClassification(
            category="retryable",
            error_type=ERR_TRANSPORT,
            log_event_type=EVT_NETWORK,
            error_message=f"Transport error: {exception}",
            http_status=None,
        )

    # Catch-all for httpx.RequestError subclasses we didn't match
    # explicitly (DecodingError, TooManyRedirects, etc.). Treat as
    # transport for retry purposes.
    if isinstance(exception, httpx.RequestError):
        return DeliveryClassification(
            category="retryable",
            error_type=ERR_TRANSPORT,
            log_event_type=EVT_NETWORK,
            error_message=f"Request error: {exception}",
            http_status=None,
        )

    # Anything else — programming error / unexpected. Treat as
    # terminal so it doesn't loop forever; the log will surface it.
    return DeliveryClassification(
        category="terminal",
        error_type="unexpected_error",
        log_event_type="msg_delivery_unexpected_error",
        error_message=f"Unexpected error: {type(exception).__name__}: {exception}",
        http_status=None,
    )
