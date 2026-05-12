"""Tests for BodyVerify Layer 1 — substrate echo-back primitive (STRING-shape spec).

Design doc: https://trydock.ai/workspaces/cue-message-silent-corruption-substrate-design-2026-05-11

Coverage targets:

- Helper ``apply_verify_echo``: header-absent zero-cost no-op, header-present
  echo-back, hash determinism, None-body handling, branch coverage.
- ``POST /v1/messages``: round-trip happy path; 6 metachar classes round-trip
  byte-identical (asserting STRING type + sha256(sent_body) == response hash);
  no-header → no echo fields (backwards-compat); empty body; 32KB cap edge.
- ``POST /v1/cues/{cue_id}/fire``: round-trip with payload_override.message
  (canonical live-cue content vector); metachar classes; no-body fire
  (FireRequest=None path) still produces echo when header set;
  payload_override without 'message' key falls back to canonical JSON dump.
- Definition of Done item 1: 6 metachar classes (backticks, $-paren, ${VAR},
  backslash, quotes, mixed).
"""
from __future__ import annotations

import hashlib
import json
import uuid

import pytest
from fastapi import Request

from app.utils.verify_echo import (
    VERIFY_ECHO_HEADER,
    apply_verify_echo,
    verify_echo_requested,
)


def _fake_request(headers: dict) -> Request:
    """Build a minimal ASGI Request stub for header-reading tests."""
    scope = {
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    return Request(scope)


# ───────────────────────────────────────────────────────────────────────
# Helper unit tests — apply_verify_echo / verify_echo_requested
# ───────────────────────────────────────────────────────────────────────


def test_verify_echo_requested_true_when_header_set():
    req = _fake_request({VERIFY_ECHO_HEADER: "true"})
    assert verify_echo_requested(req) is True


def test_verify_echo_requested_case_insensitive():
    req = _fake_request({VERIFY_ECHO_HEADER: "TRUE"})
    assert verify_echo_requested(req) is True


def test_verify_echo_requested_strips_whitespace():
    req = _fake_request({VERIFY_ECHO_HEADER: "  true  "})
    assert verify_echo_requested(req) is True


def test_verify_echo_requested_false_when_absent():
    req = _fake_request({})
    assert verify_echo_requested(req) is False


def test_verify_echo_requested_false_when_not_true():
    req = _fake_request({VERIFY_ECHO_HEADER: "1"})
    assert verify_echo_requested(req) is False
    req2 = _fake_request({VERIFY_ECHO_HEADER: "yes"})
    assert verify_echo_requested(req2) is False


def test_apply_verify_echo_returns_empty_dict_without_header():
    req = _fake_request({})
    assert apply_verify_echo(request=req, body_text="any string") == {}


def test_apply_verify_echo_none_body():
    req = _fake_request({VERIFY_ECHO_HEADER: "true"})
    result = apply_verify_echo(request=req, body_text=None)
    assert result["body_received"] is None
    # SHA256 of empty bytes is a well-known constant.
    assert result["body_received_sha256"] == hashlib.sha256(b"").hexdigest()


def test_apply_verify_echo_string_body_round_trips_byte_identical():
    """Spec: body_received is STRING; hash matches sha256(string.encode())."""
    req = _fake_request({VERIFY_ECHO_HEADER: "true"})
    body = "hello world"
    result = apply_verify_echo(request=req, body_text=body)
    assert result["body_received"] == body
    assert isinstance(result["body_received"], str)
    expected = hashlib.sha256(b"hello world").hexdigest()
    assert result["body_received_sha256"] == expected


def test_apply_verify_echo_empty_string_body():
    """Empty string is distinct from None: echo as empty string + hash of empty bytes."""
    req = _fake_request({VERIFY_ECHO_HEADER: "true"})
    result = apply_verify_echo(request=req, body_text="")
    assert result["body_received"] == ""
    assert isinstance(result["body_received"], str)
    assert result["body_received_sha256"] == hashlib.sha256(b"").hexdigest()


def test_apply_verify_echo_unicode_body_hashes_utf8():
    """Unicode body hashes match sha256 of UTF-8 encoded bytes."""
    req = _fake_request({VERIFY_ECHO_HEADER: "true"})
    body = "héllo 👋"
    result = apply_verify_echo(request=req, body_text=body)
    assert result["body_received"] == body
    assert result["body_received_sha256"] == hashlib.sha256(
        body.encode("utf-8")
    ).hexdigest()


# ───────────────────────────────────────────────────────────────────────
# Integration — POST /v1/messages
# ───────────────────────────────────────────────────────────────────────


async def _make_agent(client, headers, slug=None):
    payload = {"display_name": f"Echo Agent {uuid.uuid4().hex[:6]}", "metadata": {}}
    if slug:
        payload["slug"] = slug
    r = await client.post("/v1/agents", json=payload, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _from_header(agent):
    return {"X-Cueapi-From-Agent": agent["id"]}


@pytest.mark.asyncio
async def test_messages_no_header_no_echo_fields(client, auth_headers):
    """Backwards-compat: response without X-CueAPI-Verify-Echo has no echo fields."""
    sender = await _make_agent(client, auth_headers, slug=f"echo-noh-s-{uuid.uuid4().hex[:6]}")
    recipient = await _make_agent(client, auth_headers, slug=f"echo-noh-r-{uuid.uuid4().hex[:6]}")
    r = await client.post(
        "/v1/messages",
        json={"to": recipient["id"], "body": "plain"},
        headers={**auth_headers, **_from_header(sender)},
    )
    assert r.status_code == 201
    data = r.json()
    assert "body_received" not in data
    assert "body_received_sha256" not in data


@pytest.mark.asyncio
async def test_messages_echo_roundtrip_happy_path(client, auth_headers):
    """Spec: body_received is STRING value of MessageCreate.body — NOT envelope dict.

    Caller-side recipe: sha256(sent_body.encode()) MUST match
    response.body_received_sha256.
    """
    sender = await _make_agent(client, auth_headers, slug=f"echo-hp-s-{uuid.uuid4().hex[:6]}")
    recipient = await _make_agent(client, auth_headers, slug=f"echo-hp-r-{uuid.uuid4().hex[:6]}")
    body_text = "round-trip test body"
    r = await client.post(
        "/v1/messages",
        json={"to": recipient["id"], "body": body_text},
        headers={
            **auth_headers,
            **_from_header(sender),
            VERIFY_ECHO_HEADER: "true",
        },
    )
    assert r.status_code == 201
    data = r.json()
    # Spec lock: STRING, not envelope dict.
    assert isinstance(
        data["body_received"], str
    ), f"body_received must be str; got {type(data['body_received']).__name__}"
    assert data["body_received"] == body_text
    # Caller-side hash MUST match.
    expected_hash = hashlib.sha256(body_text.encode("utf-8")).hexdigest()
    assert data["body_received_sha256"] == expected_hash


@pytest.mark.parametrize(
    "metachar_class, payload",
    [
        ("backticks", "literal `backticks` should survive"),
        ("dollar_paren", "literal $(echo X) should survive"),
        ("dollar_brace", "literal ${VAR} should survive"),
        ("backslash", "literal \\n \\t \\\\ should survive"),
        ("quotes", "literal 'single' and \"double\" quotes should survive"),
        ("mixed", "mixed: `cmd` $(sub) ${ref} \\esc \"q\" 'q' should survive"),
    ],
)
@pytest.mark.asyncio
async def test_messages_echo_six_metachar_classes(
    client, auth_headers, metachar_class, payload
):
    """Definition of Done item 1: STRING body_received + matching sha256 across
    6 metachar classes."""
    sender = await _make_agent(
        client, auth_headers, slug=f"echo-{metachar_class[:6]}-s-{uuid.uuid4().hex[:5]}"
    )
    recipient = await _make_agent(
        client, auth_headers, slug=f"echo-{metachar_class[:6]}-r-{uuid.uuid4().hex[:5]}"
    )
    r = await client.post(
        "/v1/messages",
        json={"to": recipient["id"], "body": payload},
        headers={
            **auth_headers,
            **_from_header(sender),
            VERIFY_ECHO_HEADER: "true",
        },
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert isinstance(data["body_received"], str), f"metachar class {metachar_class}"
    assert (
        data["body_received"] == payload
    ), f"metachar class {metachar_class} did NOT survive round-trip"
    # Hash check — the substantive proof for the corruption-detection use case.
    expected_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert (
        data["body_received_sha256"] == expected_hash
    ), f"metachar class {metachar_class} sha256 mismatch"


@pytest.mark.asyncio
async def test_messages_echo_header_uppercase_value_works(client, auth_headers):
    """Header value 'TRUE' / 'True' all match (case-insensitive)."""
    sender = await _make_agent(client, auth_headers, slug=f"echo-case-s-{uuid.uuid4().hex[:6]}")
    recipient = await _make_agent(client, auth_headers, slug=f"echo-case-r-{uuid.uuid4().hex[:6]}")
    r = await client.post(
        "/v1/messages",
        json={"to": recipient["id"], "body": "case test"},
        headers={
            **auth_headers,
            **_from_header(sender),
            VERIFY_ECHO_HEADER: "True",
        },
    )
    assert r.status_code == 201
    assert "body_received" in r.json()
    assert isinstance(r.json()["body_received"], str)


@pytest.mark.asyncio
async def test_messages_echo_header_false_value_no_fields(client, auth_headers):
    """Header value 'false' (or other non-'true') → no echo fields."""
    sender = await _make_agent(client, auth_headers, slug=f"echo-false-s-{uuid.uuid4().hex[:6]}")
    recipient = await _make_agent(client, auth_headers, slug=f"echo-false-r-{uuid.uuid4().hex[:6]}")
    r = await client.post(
        "/v1/messages",
        json={"to": recipient["id"], "body": "plain"},
        headers={
            **auth_headers,
            **_from_header(sender),
            VERIFY_ECHO_HEADER: "false",
        },
    )
    assert r.status_code == 201
    assert "body_received" not in r.json()


@pytest.mark.asyncio
async def test_messages_echo_32kb_cap_edge(client, auth_headers):
    """Body at the 32KB inline cap still round-trips byte-identical (STRING)."""
    sender = await _make_agent(client, auth_headers, slug=f"echo-32k-s-{uuid.uuid4().hex[:6]}")
    recipient = await _make_agent(client, auth_headers, slug=f"echo-32k-r-{uuid.uuid4().hex[:6]}")
    large_body = "x" * (32 * 1024 - 100)
    r = await client.post(
        "/v1/messages",
        json={"to": recipient["id"], "body": large_body},
        headers={
            **auth_headers,
            **_from_header(sender),
            VERIFY_ECHO_HEADER: "true",
        },
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert isinstance(data["body_received"], str)
    assert data["body_received"] == large_body
    assert data["body_received_sha256"] == hashlib.sha256(
        large_body.encode("utf-8")
    ).hexdigest()


# ───────────────────────────────────────────────────────────────────────
# Integration — POST /v1/cues/{cue_id}/fire
# ───────────────────────────────────────────────────────────────────────


async def _create_fire_cue(client, auth_headers, name=None):
    """Create a cue we can fire repeatedly in tests."""
    n = name or f"echo-fire-{uuid.uuid4().hex[:8]}"
    r = await client.post(
        "/v1/cues",
        json={
            "name": n,
            "schedule": {"type": "recurring", "cron": "0 * * * *"},
            "callback": {"url": "https://example.com/webhook"},
            "payload": {"task": "verify-echo-test"},
        },
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.mark.asyncio
async def test_fire_no_header_no_echo_fields(client, auth_headers):
    """Backwards-compat: fire response without header has no echo fields."""
    cue_id = await _create_fire_cue(client, auth_headers)
    r = await client.post(f"/v1/cues/{cue_id}/fire", headers=auth_headers)
    assert r.status_code == 200
    data = r.json()
    assert "body_received" not in data
    assert "body_received_sha256" not in data


# Note: payload_override-based fire metachar tests are intentionally OSS-omitted.
# OSS FireRequest carries only `send_at` (datetime) — no string user-content
# field where shell-expansion corruption could occur. Hosted's payload_override
# (dict with user-supplied strings) is hosted-only; the metachar-round-trip
# discipline IS exercised on the /v1/messages endpoint above (same substrate
# helper, same six classes). See parity-manifest ``oss_only_exclusions``.


@pytest.mark.asyncio
async def test_fire_echo_no_body_returns_none_echo(client, auth_headers):
    """Header set on no-body fire → body_received=None + sha256 of empty bytes."""
    cue_id = await _create_fire_cue(client, auth_headers)
    r = await client.post(
        f"/v1/cues/{cue_id}/fire",
        headers={**auth_headers, VERIFY_ECHO_HEADER: "true"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["body_received"] is None
    assert data["body_received_sha256"] == hashlib.sha256(b"").hexdigest()


@pytest.mark.asyncio
async def test_fire_echo_with_send_at_body_still_none(client, auth_headers):
    """OSS FireRequest body (send_at) carries no string user-content; echo None.

    OSS-specific: the fire endpoint passes ``body_text=None`` to the helper
    regardless of whether ``send_at`` is set, since send_at is a datetime not
    a corruption-vulnerable string. Caller-side sha256(send_at_iso) does NOT
    need to match anything — verify-echo on the fire path is a no-op contract
    on OSS until a content-bearing field is added.
    """
    from datetime import datetime, timedelta, timezone
    cue_id = await _create_fire_cue(client, auth_headers)
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    r = await client.post(
        f"/v1/cues/{cue_id}/fire",
        json={"send_at": future},
        headers={**auth_headers, VERIFY_ECHO_HEADER: "true"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["body_received"] is None
    assert data["body_received_sha256"] == hashlib.sha256(b"").hexdigest()


@pytest.mark.asyncio
async def test_fire_echo_preserves_original_response_fields(client, auth_headers):
    """Echo fields are additive — existing fire response shape unchanged."""
    cue_id = await _create_fire_cue(client, auth_headers)
    r = await client.post(
        f"/v1/cues/{cue_id}/fire",
        headers={**auth_headers, VERIFY_ECHO_HEADER: "true"},
    )
    assert r.status_code == 200
    data = r.json()
    assert "id" in data
    assert "cue_id" in data
    assert data["cue_id"] == cue_id
    assert data["status"] == "pending"
    assert data["triggered_by"] == "manual_fire"
    assert "body_received" in data
    assert "body_received_sha256" in data
