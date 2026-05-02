"""Slice 3a unit tests — classification + Retry-After parsing.

Spec: <https://trydock.ai/mike/cueapi-messaging-primitive-v1-sp> §5.4.

Pins the classification taxonomy that ``deliver_message_task`` and
``retry_message_task`` (Slice 3b) will route on. Each row in the
spec §5.4 classification table maps to one or more cases here.
"""
from __future__ import annotations

import socket
import ssl

import httpx
import pytest

from app.services.message_classification import (
    EVT_4XX_TERMINAL,
    EVT_5XX,
    EVT_429_RETRY_AFTER,
    EVT_CONN_REFUSED,
    EVT_DELIVERED,
    EVT_DNS_FAIL,
    EVT_NETWORK,
    EVT_TIMEOUT,
    EVT_TLS_FAIL,
    classify_exception,
    classify_response,
)
from app.utils.retry_after import parse_retry_after


# ── classify_response (HTTP status codes) ──────────────────────────


@pytest.mark.parametrize("status", [200, 201, 202, 204, 299])
def test_2xx_is_success_terminal(status):
    c = classify_response(status)
    assert c.category == "success"
    assert c.log_event_type == EVT_DELIVERED
    assert c.is_success
    assert not c.is_retryable
    assert not c.is_terminal


def test_401_is_terminal_auth_failed():
    c = classify_response(401)
    assert c.category == "terminal"
    assert c.error_type == "auth_failed"
    assert c.log_event_type == EVT_4XX_TERMINAL
    assert "webhook_secret" in c.error_message


def test_404_is_terminal_endpoint_missing():
    c = classify_response(404)
    assert c.category == "terminal"
    assert c.error_type == "endpoint_missing"
    assert "webhook_url" in c.error_message


def test_405_is_terminal_method_not_allowed():
    c = classify_response(405)
    assert c.category == "terminal"
    assert c.error_type == "method_not_allowed"


@pytest.mark.parametrize("status", [400, 406, 410, 412, 422, 451])
def test_other_4xx_is_terminal_client_error(status):
    c = classify_response(status)
    assert c.category == "terminal"
    assert c.error_type == "client_error"


def test_408_is_retryable_request_timeout():
    c = classify_response(408)
    assert c.category == "retryable"
    assert c.error_type == "request_timeout"


def test_429_is_retryable_rate_limited():
    c = classify_response(429)
    assert c.category == "retryable"
    assert c.error_type == "rate_limited"
    assert c.log_event_type == EVT_429_RETRY_AFTER


def test_502_is_retryable_bad_gateway():
    c = classify_response(502)
    assert c.category == "retryable"
    assert c.error_type == "bad_gateway"
    assert c.log_event_type == EVT_5XX
    assert "proxy" in c.error_message.lower()


def test_503_is_retryable_service_unavailable():
    c = classify_response(503)
    assert c.category == "retryable"
    assert c.error_type == "service_unavailable"


@pytest.mark.parametrize("status", [500, 504, 599])
def test_other_5xx_is_retryable_server_error(status):
    c = classify_response(status)
    assert c.category == "retryable"
    assert c.error_type == "server_error"


@pytest.mark.parametrize("status", [100, 101, 301, 302, 304, 600])
def test_unexpected_status_is_terminal(status):
    """1xx / 3xx / out-of-range → terminal (don't retry unknowns)."""
    c = classify_response(status)
    assert c.category == "terminal"


# ── classify_exception (transport-level failures) ──────────────────


def test_timeout_exception():
    exc = httpx.ReadTimeout("read timed out")
    c = classify_exception(exc)
    assert c.category == "retryable"
    assert c.error_type == "timeout"
    assert c.log_event_type == EVT_TIMEOUT
    assert c.http_status is None


def test_connect_timeout_is_timeout():
    """ConnectTimeout subclasses TimeoutException; classifier
    matches TimeoutException first, so it routes to ``timeout``
    rather than ``connection_refused``.
    """
    exc = httpx.ConnectTimeout("connect timed out")
    c = classify_exception(exc)
    assert c.error_type == "timeout"


def test_tls_handshake_failure_via_ssl_cause():
    """httpx wraps ssl.SSLError inside httpx.ConnectError. Inspect
    __cause__ to distinguish TLS failure from TCP refused / DNS.
    """
    underlying = ssl.SSLError("certificate verify failed: cert expired")
    wrapper = httpx.ConnectError("ssl handshake failed")
    wrapper.__cause__ = underlying
    c = classify_exception(wrapper)
    assert c.category == "retryable"
    assert c.error_type == "tls_handshake_failed"
    assert c.log_event_type == EVT_TLS_FAIL
    assert "cert expired" in c.error_message


def test_dns_resolution_failure_via_gaierror_cause():
    """socket.gaierror surfaces as httpx.ConnectError with the
    gaierror as __cause__. Distinct error_type from connection
    refused for observability per Max's review.
    """
    underlying = socket.gaierror(-2, "Name or service not known")
    wrapper = httpx.ConnectError("dns lookup failed")
    wrapper.__cause__ = underlying
    c = classify_exception(wrapper)
    assert c.category == "retryable"
    assert c.error_type == "dns_resolution_failed"
    assert c.log_event_type == EVT_DNS_FAIL


def test_connect_error_without_special_cause_is_connection_refused():
    """ConnectError with no SSLError / gaierror cause → generic
    connection refused / unreachable bucket.
    """
    exc = httpx.ConnectError("Connection refused")
    c = classify_exception(exc)
    assert c.category == "retryable"
    assert c.error_type == "connection_refused"
    assert c.log_event_type == EVT_CONN_REFUSED


def test_network_error_subclass():
    """ReadError / WriteError / CloseError — connection broke
    mid-stream.
    """
    exc = httpx.ReadError("connection broke")
    c = classify_exception(exc)
    assert c.category == "retryable"
    assert c.error_type == "network"
    assert c.log_event_type == EVT_NETWORK


def test_protocol_error():
    """RemoteProtocolError surfaces e.g. when recipient closes
    abruptly mid-response. Retry — likely transient.
    """
    exc = httpx.RemoteProtocolError("protocol violation")
    c = classify_exception(exc)
    assert c.category == "retryable"
    # Falls into transport bucket since it's a TransportError
    # subclass that doesn't match earlier checks.
    assert c.error_type == "transport"


def test_unexpected_exception_is_terminal():
    """Programming errors / unexpected exceptions are terminal —
    don't retry unknowns."""
    exc = ValueError("something went wrong")
    c = classify_exception(exc)
    assert c.category == "terminal"
    assert c.error_type == "unexpected_error"
    assert "ValueError" in c.error_message


# ── parse_retry_after (RFC 7231 §7.1.3) ────────────────────────────


def test_retry_after_absent_returns_own_min():
    assert parse_retry_after(None, own_min_seconds=60) == 60
    assert parse_retry_after("", own_min_seconds=60) == 60
    assert parse_retry_after("   ", own_min_seconds=60) == 60


def test_retry_after_zero_respects_own_min():
    """Recipient saying 'retry immediately' must NOT bypass server's
    polite minimum backoff. max(60, 0) = 60.
    """
    assert parse_retry_after("0", own_min_seconds=60) == 60


def test_retry_after_smaller_than_own_min_uses_own_min():
    """Retry-After: 30 with own_min: 60 → server's 60s wins."""
    assert parse_retry_after("30", own_min_seconds=60) == 60


def test_retry_after_larger_than_own_min_uses_retry_after():
    """Retry-After: 600 with own_min: 60 → recipient's 600s wins."""
    assert parse_retry_after("600", own_min_seconds=60) == 600


def test_retry_after_exactly_equals_own_min():
    """max(60, 60) = 60 — boundary correctness."""
    assert parse_retry_after("60", own_min_seconds=60) == 60


def test_retry_after_negative_treated_as_absent():
    """Negative Retry-After is malformed; fall back to own_min."""
    assert parse_retry_after("-1", own_min_seconds=60) == 60


def test_retry_after_non_numeric_treated_as_absent():
    """Decimals / arbitrary strings / HTTP-date form (rare) all
    fall back to own_min — v1.5 supports seconds-as-int only.
    """
    assert parse_retry_after("60.5", own_min_seconds=60) == 60
    assert parse_retry_after("five", own_min_seconds=60) == 60
    assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT", own_min_seconds=60) == 60


def test_retry_after_strips_whitespace():
    """Tolerates ``Retry-After:  60`` with extra padding from the
    recipient's HTTP stack.
    """
    assert parse_retry_after("  60  ", own_min_seconds=60) == 60
    assert parse_retry_after("\t300\n", own_min_seconds=60) == 300


def test_retry_after_with_zero_floor():
    """If own_min is 0 (paranoid: should never happen in production
    but guard regardless), Retry-After: 0 still returns 0.
    """
    assert parse_retry_after("0", own_min_seconds=0) == 0
    assert parse_retry_after("100", own_min_seconds=0) == 100
