"""Tests for BodyVerify Layer 1 — substrate echo-back primitive.

Design doc: https://trydock.ai/workspaces/cue-message-silent-corruption-substrate-design-2026-05-11

Coverage targets:

- Helper ``apply_verify_echo``: header-absent zero-cost no-op, header-present
  echo-back, hash determinism, branch coverage (None / Pydantic model / dict /
  other), canonical JSON hashing (sorted keys, no whitespace).
- ``POST /v1/messages``: round-trip happy path; 6 metachar classes round-trip
  byte-identical; no-header → no echo fields (backwards-compat); empty body
  (well-formed but minimal content); 32KB cap edge.
- ``POST /v1/cues/{cue_id}/fire``: round-trip with payload_override; metachar
  classes; no-body fire (FireRequest=None path) still produces echo when header
  set.
- Definition of Done item 1 (substrate echo-back): 6 metachar classes assertion
  matrix covering backticks, $-paren, ${VAR}, backslash, quotes, mixed.
"""
from __future__ import annotations

import hashlib
import json
import uuid

import pytest
from fastapi import Request

from app.utils.verify_echo import (
    VERIFY_ECHO_HEADER,
    _canonical_json_bytes,
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
    assert apply_verify_echo(request=req, parsed_body={"any": "value"}) == {}


def test_apply_verify_echo_none_body():
    req = _fake_request({VERIFY_ECHO_HEADER: "true"})
    result = apply_verify_echo(request=req, parsed_body=None)
    assert result["body_received"] is None
    # SHA256 of empty bytes is a well-known constant.
    assert result["body_received_sha256"] == hashlib.sha256(b"").hexdigest()


def test_apply_verify_echo_dict_body():
    req = _fake_request({VERIFY_ECHO_HEADER: "true"})
    body = {"message": "hello", "priority": 3}
    result = apply_verify_echo(request=req, parsed_body=body)
    assert result["body_received"] == body
    expected = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert result["body_received_sha256"] == expected


def test_apply_verify_echo_pydantic_model_body():
    from pydantic import BaseModel

    class _M(BaseModel):
        a: str
        b: int

    req = _fake_request({VERIFY_ECHO_HEADER: "true"})
    model = _M(a="x", b=42)
    result = apply_verify_echo(request=req, parsed_body=model)
    assert result["body_received"] == {"a": "x", "b": 42}
    assert isinstance(result["body_received_sha256"], str)
    assert len(result["body_received_sha256"]) == 64


def test_apply_verify_echo_hash_deterministic_across_key_order():
    """Canonical JSON (sorted keys) means {a,b} and {b,a} hash identically."""
    req = _fake_request({VERIFY_ECHO_HEADER: "true"})
    r1 = apply_verify_echo(request=req, parsed_body={"a": 1, "b": 2})
    r2 = apply_verify_echo(request=req, parsed_body={"b": 2, "a": 1})
    assert r1["body_received_sha256"] == r2["body_received_sha256"]


def test_apply_verify_echo_other_type_body():
    req = _fake_request({VERIFY_ECHO_HEADER: "true"})
    result = apply_verify_echo(request=req, parsed_body=12345)
    assert result["body_received"] == "12345"
    assert result["body_received_sha256"] == hashlib.sha256(b"12345").hexdigest()


def test_canonical_json_bytes_unicode_preserved():
    """ensure_ascii=False → unicode chars round-trip byte-faithful in hash."""
    out = _canonical_json_bytes({"msg": "héllo"})
    assert b"h\xc3\xa9llo" in out


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
    """Header present → response includes body_received matching sent body."""
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
    assert "body_received" in data
    assert data["body_received"]["body"] == body_text
    assert data["body_received"]["to"] == recipient["id"]
    assert isinstance(data["body_received_sha256"], str)
    assert len(data["body_received_sha256"]) == 64


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
    """Definition of Done item 1: byte-identical round-trip for 6 metachar classes."""
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
    assert (
        data["body_received"]["body"] == payload
    ), f"metachar class {metachar_class} did NOT survive round-trip"


@pytest.mark.asyncio
async def test_messages_echo_header_lowercase_value_works(client, auth_headers):
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
    """Body at the 32KB inline cap still round-trips byte-identical."""
    sender = await _make_agent(client, auth_headers, slug=f"echo-32k-s-{uuid.uuid4().hex[:6]}")
    recipient = await _make_agent(client, auth_headers, slug=f"echo-32k-r-{uuid.uuid4().hex[:6]}")
    # 32 KB minus a safety margin so we sit JUST under the limit
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
    assert data["body_received"]["body"] == large_body
    assert len(data["body_received"]["body"]) == len(large_body)


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


@pytest.mark.asyncio
async def test_fire_echo_with_send_at(client, auth_headers):
    """Header present + FireRequest body → response echoes parsed FireRequest.

    Note: OSS ``FireRequest`` carries only ``send_at`` (datetime). Hosted's
    ``payload_override`` (dict with user content) is the corruption vector
    on the fire path and lives in cueapi/cueapi only. This test exercises
    the substrate echo-back against whatever fields OSS ``FireRequest``
    exposes; metachar parametrization on the fire path is intentionally
    private-only (see parity-manifest deviation note).
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
    assert "body_received" in data
    # FireRequest.send_at is the only OSS field; assert it round-trips.
    assert data["body_received"]["send_at"] is not None
    assert len(data["body_received_sha256"]) == 64


@pytest.mark.asyncio
async def test_fire_echo_no_body_returns_none_echo(client, auth_headers):
    """Header set but no fire request body → body_received=None (FireRequest=None path)."""
    cue_id = await _create_fire_cue(client, auth_headers)
    r = await client.post(
        f"/v1/cues/{cue_id}/fire",
        headers={**auth_headers, VERIFY_ECHO_HEADER: "true"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert "body_received" in data
    assert data["body_received"] is None
    # SHA256 of empty bytes
    assert (
        data["body_received_sha256"]
        == hashlib.sha256(b"").hexdigest()
    )


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
    # Original response shape preserved
    assert "id" in data
    assert "cue_id" in data
    assert data["cue_id"] == cue_id
    assert data["status"] == "pending"
    assert data["triggered_by"] == "manual_fire"
    # Echo fields additionally present
    assert "body_received" in data
    assert "body_received_sha256" in data


# Note: metachar-class parametrization on the fire path is private-only.
# OSS ``FireRequest`` carries only ``send_at`` (datetime); hosted's
# ``payload_override`` (dict with user-supplied string content) is the
# corruption vector and lives in cueapi/cueapi exclusively. See
# parity-manifest ``oss_only_exclusions`` for the deviation note.
# The metachar-class round-trip discipline IS exercised on the
# /v1/messages endpoint above — same substrate helper, same six classes.
