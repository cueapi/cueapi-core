"""Tests for BodyVerify Layer 1.5 — universal middleware.

Phase 1 wired echo-back per-handler on POST /v1/messages + POST /v1/cues/<id>/fire.
This phase ships ``VerifyEchoMiddleware`` so the primitive applies to every
POST/PATCH/PUT JSON endpoint without per-handler integration.

Coverage targets:

- ``VerifyEchoMiddleware`` happy path: header present + dict response → injects
  ``body_received`` + ``body_received_sha256`` on at least 3 representative
  endpoints (POST /v1/agents, POST /v1/cues, PATCH /v1/auth/me).
- Backwards-compat: header absent → no echo fields. GET unaffected.
- Method gating: header set on GET → no echo (only POST/PATCH/PUT).
- Status gating: 4xx/5xx responses NOT echoed (validation errors stay clean).
- Content-type gating: non-JSON 2xx responses NOT echoed (e.g. HTML pages).
- Idempotency: Phase 1 endpoints (messages + fire) already echo; middleware
  must NOT double-inject — existing ``body_received`` wins.
- Raw body preservation: invalid JSON body still passes through but echo is
  the raw string + hash of raw bytes.
- 6 metachar classes on a representative non-Phase-1 endpoint (POST /v1/agents
  via the display_name field).
- Empty body handling.
"""
from __future__ import annotations

import hashlib
import uuid

import pytest


VERIFY_ECHO_HEADER_KEY = "X-CueAPI-Verify-Echo"


# ───────────────────────────────────────────────────────────────────────
# Backwards-compat — header absent path
# ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_header_no_echo_on_agents_create(client, auth_headers):
    """Default behavior: POST /v1/agents without header has no echo fields."""
    r = await client.post(
        "/v1/agents",
        json={"display_name": f"NoEcho {uuid.uuid4().hex[:6]}", "metadata": {}},
        headers=auth_headers,
    )
    assert r.status_code == 201
    data = r.json()
    assert "body_received" not in data
    assert "body_received_sha256" not in data


@pytest.mark.asyncio
async def test_no_header_no_echo_on_cue_create(client, auth_headers):
    """Default behavior: POST /v1/cues without header has no echo fields."""
    r = await client.post(
        "/v1/cues",
        json={
            "name": f"no-echo-{uuid.uuid4().hex[:6]}",
            "schedule": {"type": "recurring", "cron": "0 * * * *"},
            "callback": {"url": "https://example.com/webhook"},
            "payload": {"task": "test"},
        },
        headers=auth_headers,
    )
    assert r.status_code == 201
    data = r.json()
    assert "body_received" not in data
    assert "body_received_sha256" not in data


# ───────────────────────────────────────────────────────────────────────
# Happy path — header present + dict response → echo fields injected
# ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_echo_on_agents_create(client, auth_headers):
    """POST /v1/agents with X-CueAPI-Verify-Echo: true → echo fields present."""
    display = f"EchoAgent {uuid.uuid4().hex[:6]}"
    r = await client.post(
        "/v1/agents",
        json={"display_name": display, "metadata": {}},
        headers={**auth_headers, VERIFY_ECHO_HEADER_KEY: "true"},
    )
    assert r.status_code == 201
    data = r.json()
    assert "body_received" in data
    assert data["body_received"]["display_name"] == display
    assert isinstance(data["body_received_sha256"], str)
    assert len(data["body_received_sha256"]) == 64


@pytest.mark.asyncio
async def test_echo_on_cue_create(client, auth_headers):
    """POST /v1/cues with X-CueAPI-Verify-Echo: true → echo fields present."""
    name = f"echo-cue-{uuid.uuid4().hex[:6]}"
    r = await client.post(
        "/v1/cues",
        json={
            "name": name,
            "schedule": {"type": "recurring", "cron": "0 * * * *"},
            "callback": {"url": "https://example.com/webhook"},
            "payload": {"task": "test"},
        },
        headers={**auth_headers, VERIFY_ECHO_HEADER_KEY: "true"},
    )
    assert r.status_code == 201
    data = r.json()
    assert "body_received" in data
    assert data["body_received"]["name"] == name
    assert len(data["body_received_sha256"]) == 64


# ───────────────────────────────────────────────────────────────────────
# Method gating — only POST/PATCH/PUT
# ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_request_no_echo_even_with_header(client, auth_headers):
    """GET requests bypass the middleware — header set, no fields injected."""
    r = await client.get(
        "/v1/usage",
        headers={**auth_headers, VERIFY_ECHO_HEADER_KEY: "true"},
    )
    assert r.status_code == 200
    data = r.json()
    assert "body_received" not in data
    assert "body_received_sha256" not in data


# ───────────────────────────────────────────────────────────────────────
# Status gating — non-2xx responses should NOT carry echo fields
# ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validation_error_no_echo(client, auth_headers):
    """422 / 4xx validation responses stay clean — middleware doesn't inject."""
    r = await client.post(
        "/v1/agents",
        json={},  # missing required display_name
        headers={**auth_headers, VERIFY_ECHO_HEADER_KEY: "true"},
    )
    assert r.status_code in (400, 422)
    data = r.json()
    assert "body_received" not in data
    assert "body_received_sha256" not in data


# ───────────────────────────────────────────────────────────────────────
# Idempotency — Phase 1 endpoints already echo; middleware must not double
# ───────────────────────────────────────────────────────────────────────


async def _make_agent(client, headers, slug=None):
    payload = {"display_name": f"Agent {uuid.uuid4().hex[:6]}", "metadata": {}}
    if slug:
        payload["slug"] = slug
    r = await client.post("/v1/agents", json=payload, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _from_header(agent):
    return {"X-Cueapi-From-Agent": agent["id"]}


@pytest.mark.asyncio
async def test_phase_1_messages_endpoint_idempotent_under_middleware(
    client, auth_headers
):
    """Phase 1's send_message handler injects body_received as the STRING value
    of MessageCreate.body (per design-lock hotfix). Middleware must NOT
    overwrite the handler-supplied shape — Phase 1 STRING wins over middleware's
    parsed-JSON-dict echo via idempotency.

    Sentinel: ``body_received`` is a STRING equal to the sent body, not a dict.
    """
    import hashlib
    sender = await _make_agent(
        client, auth_headers, slug=f"idem-s-{uuid.uuid4().hex[:6]}"
    )
    recipient = await _make_agent(
        client, auth_headers, slug=f"idem-r-{uuid.uuid4().hex[:6]}"
    )
    body_text = "idem test"
    r = await client.post(
        "/v1/messages",
        json={"to": recipient["id"], "body": body_text},
        headers={
            **auth_headers,
            **_from_header(sender),
            VERIFY_ECHO_HEADER_KEY: "true",
        },
    )
    assert r.status_code == 201
    data = r.json()
    assert "body_received" in data
    assert isinstance(data["body_received"], str), (
        f"Phase 1 idempotency violated: body_received should be STRING (handler "
        f"shape); got {type(data['body_received']).__name__} — middleware "
        f"overwrote handler-supplied echo."
    )
    assert data["body_received"] == body_text
    assert data["body_received_sha256"] == hashlib.sha256(
        body_text.encode("utf-8")
    ).hexdigest()


# ───────────────────────────────────────────────────────────────────────
# 6 metachar classes on a non-Phase-1 endpoint
# ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "metachar_class, payload_value",
    [
        ("backticks", "agent `backticks` literal"),
        ("dollar_paren", "agent $(echo X) literal"),
        ("dollar_brace", "agent ${VAR} literal"),
        ("backslash", "agent \\n \\t literal"),
        ("quotes", "agent 'single' and \"double\" literal"),
        ("mixed", "agent mixed `cmd` $(sub) ${ref} \\esc \"q\" 'q' literal"),
    ],
)
@pytest.mark.asyncio
async def test_echo_six_metachar_classes_on_agents_create(
    client, auth_headers, metachar_class, payload_value
):
    """Definition of Done item 1 extension: byte-identical round-trip on a
    non-Phase-1 endpoint exercised by the middleware."""
    r = await client.post(
        "/v1/agents",
        json={"display_name": payload_value, "metadata": {}},
        headers={**auth_headers, VERIFY_ECHO_HEADER_KEY: "true"},
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert (
        data["body_received"]["display_name"] == payload_value
    ), f"metachar class {metachar_class} did NOT survive middleware echo"


# ───────────────────────────────────────────────────────────────────────
# Empty body handling
# ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_echo_empty_body_post(client, auth_headers, db_session):
    """POST with no body + verify-echo header: server returns 200, echo is
    body_received=None + SHA256 of empty bytes."""
    # Create a worker-transport cue we can fire without body
    cr = await client.post(
        "/v1/cues",
        json={
            "name": f"empty-body-{uuid.uuid4().hex[:6]}",
            "schedule": {"type": "recurring", "cron": "0 * * * *"},
            "transport": "worker",
            "payload": {"task": "test"},
        },
        headers=auth_headers,
    )
    assert cr.status_code == 201, cr.text
    cue_id = cr.json()["id"]
    # Fire with no body — Phase 1's per-handler echo handles this case;
    # middleware should see existing body_received and skip.
    r = await client.post(
        f"/v1/cues/{cue_id}/fire",
        headers={**auth_headers, VERIFY_ECHO_HEADER_KEY: "true"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert "body_received" in data
    assert data["body_received"] is None
    assert data["body_received_sha256"] == hashlib.sha256(b"").hexdigest()


# ───────────────────────────────────────────────────────────────────────
# Case-insensitive header value
# ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_header_value_case_insensitive(client, auth_headers):
    """Header value 'TRUE' / 'True' triggers echo (case-insensitive match)."""
    r = await client.post(
        "/v1/agents",
        json={"display_name": f"CaseAgent {uuid.uuid4().hex[:6]}", "metadata": {}},
        headers={**auth_headers, VERIFY_ECHO_HEADER_KEY: "TRUE"},
    )
    assert r.status_code == 201
    assert "body_received" in r.json()


@pytest.mark.asyncio
async def test_header_value_non_true_no_echo(client, auth_headers):
    """Header value 'false' / '1' / 'yes' → middleware bypasses."""
    for val in ("false", "1", "yes", ""):
        r = await client.post(
            "/v1/agents",
            json={
                "display_name": f"NonTrue {uuid.uuid4().hex[:6]}",
                "metadata": {},
            },
            headers={**auth_headers, VERIFY_ECHO_HEADER_KEY: val},
        )
        assert r.status_code == 201, f"value={val!r}: {r.text}"
        assert (
            "body_received" not in r.json()
        ), f"value={val!r} unexpectedly triggered echo"


# ───────────────────────────────────────────────────────────────────────
# Direct unit tests on _maybe_inject — covers branch logic without going
# through ASGI dispatch (per CLAUDE.md ASGI coverage discipline).
# ───────────────────────────────────────────────────────────────────────


def _ct_header(value: str) -> list[tuple[bytes, bytes]]:
    return [(b"content-type", value.encode("ascii")), (b"content-length", b"99")]


def test_maybe_inject_non_2xx_returns_unchanged():
    """4xx/5xx responses bypass injection."""
    from app.middleware.verify_echo import VerifyEchoMiddleware

    result = VerifyEchoMiddleware._maybe_inject(
        status_code=422,
        headers=_ct_header("application/json"),
        request_body_bytes=b'{"x": 1}',
        response_body_bytes=b'{"detail": "validation_error"}',
    )
    # Headers and body unchanged
    assert result["body"] == b'{"detail": "validation_error"}'


def test_maybe_inject_non_json_content_type_returns_unchanged():
    """HTML/text responses bypass injection."""
    from app.middleware.verify_echo import VerifyEchoMiddleware

    result = VerifyEchoMiddleware._maybe_inject(
        status_code=200,
        headers=_ct_header("text/html; charset=utf-8"),
        request_body_bytes=b'{"x": 1}',
        response_body_bytes=b"<html>ok</html>",
    )
    assert result["body"] == b"<html>ok</html>"


def test_maybe_inject_non_dict_json_response_returns_unchanged():
    """JSON array / scalar responses bypass injection."""
    from app.middleware.verify_echo import VerifyEchoMiddleware

    result = VerifyEchoMiddleware._maybe_inject(
        status_code=200,
        headers=_ct_header("application/json"),
        request_body_bytes=b'{"x": 1}',
        response_body_bytes=b'[1, 2, 3]',
    )
    assert result["body"] == b'[1, 2, 3]'


def test_maybe_inject_malformed_response_json_returns_unchanged():
    """Defensive — response not parseable as JSON despite content-type."""
    from app.middleware.verify_echo import VerifyEchoMiddleware

    result = VerifyEchoMiddleware._maybe_inject(
        status_code=200,
        headers=_ct_header("application/json"),
        request_body_bytes=b'{"x": 1}',
        response_body_bytes=b'not valid json',
    )
    assert result["body"] == b'not valid json'


def test_maybe_inject_existing_body_received_preserved():
    """Phase 1 handler-supplied body_received wins over middleware injection."""
    from app.middleware.verify_echo import VerifyEchoMiddleware
    import json as _j

    existing = {
        "id": "msg_x",
        "body_received": {"body": "phase-1-shape"},
        "body_received_sha256": "phase-1-hash",
    }
    result = VerifyEchoMiddleware._maybe_inject(
        status_code=201,
        headers=_ct_header("application/json"),
        request_body_bytes=b'{"body": "raw-shape"}',
        response_body_bytes=_j.dumps(existing).encode("utf-8"),
    )
    # Body unchanged — Phase 1 view preserved
    parsed = _j.loads(result["body"])
    assert parsed["body_received"] == {"body": "phase-1-shape"}
    assert parsed["body_received_sha256"] == "phase-1-hash"


def test_maybe_inject_empty_request_body():
    """No request body + header set → body_received=None + SHA256 empty."""
    from app.middleware.verify_echo import VerifyEchoMiddleware
    import json as _j

    result = VerifyEchoMiddleware._maybe_inject(
        status_code=200,
        headers=_ct_header("application/json"),
        request_body_bytes=b'',
        response_body_bytes=b'{"id": "x"}',
    )
    parsed = _j.loads(result["body"])
    assert parsed["body_received"] is None
    assert parsed["body_received_sha256"] == hashlib.sha256(b"").hexdigest()


def test_maybe_inject_invalid_json_request_body_falls_back_to_string():
    """Malformed JSON request body → body_received is decoded string + raw-bytes hash."""
    from app.middleware.verify_echo import VerifyEchoMiddleware
    import json as _j

    raw = b'this is not valid {{{ json'
    result = VerifyEchoMiddleware._maybe_inject(
        status_code=200,
        headers=_ct_header("application/json"),
        request_body_bytes=raw,
        response_body_bytes=b'{"id": "x"}',
    )
    parsed = _j.loads(result["body"])
    assert parsed["body_received"] == raw.decode("utf-8")
    assert parsed["body_received_sha256"] == hashlib.sha256(raw).hexdigest()


def test_maybe_inject_content_length_header_updated():
    """When echo fields are injected, Content-Length is recomputed."""
    from app.middleware.verify_echo import VerifyEchoMiddleware

    headers_in = [
        (b"content-type", b"application/json"),
        (b"content-length", b"10"),  # stale
        (b"x-custom", b"preserved"),
    ]
    result = VerifyEchoMiddleware._maybe_inject(
        status_code=200,
        headers=headers_in,
        request_body_bytes=b'{"k": "v"}',
        response_body_bytes=b'{"id": "x"}',
    )
    # New body is longer than original (echo fields added)
    assert len(result["body"]) > 10
    # Content-Length updated to match new body
    cl = None
    for k, v in result["headers"]:
        if k.lower() == b"content-length":
            cl = int(v)
    assert cl == len(result["body"])
    # Other headers preserved
    custom = next((v for k, v in result["headers"] if k.lower() == b"x-custom"), None)
    assert custom == b"preserved"
