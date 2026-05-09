"""HTTP-level tests for the agent_live_sessions router.

Covers:

* POST /v1/agents/{ref}/live-sessions — register
* GET /v1/agents/{ref}/live-sessions — list (active + include_detached)
* DELETE /v1/agents/{ref}/live-sessions/{label} — soft-detach
* PATCH /v1/agents/{ref}/live-sessions/{label} — flip is_default
  atomically + rotate session_token

Per substrate review Q5d, the partial-unique-index re-attach semantics
are critical for correctness:

* test_relabel_after_detach — detach session A label="main", attach
  fresh session B label="main"; should succeed because the partial
  unique index uses ``WHERE detached_at IS NULL``.
* test_redefault_after_detach — detach the default session; the new
  registration with is_default=true succeeds even though the audit-
  trail row had is_default=true at attach time.

Cross-user isolation: agents owned by another user return 404 (not
403) on every endpoint.
"""
from __future__ import annotations

import uuid
from typing import Optional

import pytest


# ─── Helpers ────────────────────────────────────────────────────────


async def _make_agent(client, headers, slug: Optional[str] = None) -> dict:
    payload = {"display_name": f"Agent {uuid.uuid4().hex[:6]}", "metadata": {}}
    if slug:
        payload["slug"] = slug
    r = await client.post("/v1/agents", json=payload, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _register_body(label="main", cue_id=None, task_name=None, **kwargs) -> dict:
    body = {
        "label": label,
        "cue_id": cue_id or f"cue_{uuid.uuid4().hex[:12]}",
        "task_name": task_name or f"task-{uuid.uuid4().hex[:8]}-live",
    }
    body.update(kwargs)
    return body


# ─── Register ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_register_minimal(client, auth_headers):
    agent = await _make_agent(client, auth_headers)
    r = await client.post(
        f"/v1/agents/{agent['id']}/live-sessions",
        json=_register_body(),
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["label"] == "main"
    assert body["is_default"] is False
    assert body["attached"] is True
    assert body["attached_at"] is not None
    assert body["session_token"] is None


@pytest.mark.asyncio
async def test_register_with_default_and_token(client, auth_headers):
    agent = await _make_agent(client, auth_headers)
    body = _register_body(
        is_default=True,
        monitor_version="v2.1.0",
        session_token="01HZWC4KGE7ZYAZQX8JBQK9MPN",
    )
    r = await client.post(
        f"/v1/agents/{agent['id']}/live-sessions",
        json=body,
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    resp = r.json()
    assert resp["is_default"] is True
    assert resp["monitor_version"] == "v2.1.0"
    assert resp["session_token"] == "01HZWC4KGE7ZYAZQX8JBQK9MPN"


@pytest.mark.asyncio
async def test_register_duplicate_label_fails(client, auth_headers):
    agent = await _make_agent(client, auth_headers)
    body = _register_body(label="main")
    r1 = await client.post(
        f"/v1/agents/{agent['id']}/live-sessions",
        json=body,
        headers=auth_headers,
    )
    assert r1.status_code == 201
    # Same label, fresh cue_id — should still 409 because label is
    # unique per agent among ACTIVE sessions.
    body2 = _register_body(label="main")
    r2 = await client.post(
        f"/v1/agents/{agent['id']}/live-sessions",
        json=body2,
        headers=auth_headers,
    )
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "live_session_conflict"


@pytest.mark.asyncio
async def test_register_duplicate_cue_id_fails_globally(client, auth_headers):
    agent_a = await _make_agent(client, auth_headers)
    agent_b = await _make_agent(client, auth_headers)
    cue_id = f"cue_{uuid.uuid4().hex[:12]}"
    r1 = await client.post(
        f"/v1/agents/{agent_a['id']}/live-sessions",
        json=_register_body(cue_id=cue_id, label="a"),
        headers=auth_headers,
    )
    assert r1.status_code == 201
    # Same cue_id on a DIFFERENT agent should still 409 because cue_id
    # is globally unique.
    r2 = await client.post(
        f"/v1/agents/{agent_b['id']}/live-sessions",
        json=_register_body(cue_id=cue_id, label="b"),
        headers=auth_headers,
    )
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_register_two_defaults_fails(client, auth_headers):
    agent = await _make_agent(client, auth_headers)
    r1 = await client.post(
        f"/v1/agents/{agent['id']}/live-sessions",
        json=_register_body(label="a", is_default=True),
        headers=auth_headers,
    )
    assert r1.status_code == 201
    # Second register with is_default=true should 409 — DB-enforced.
    r2 = await client.post(
        f"/v1/agents/{agent['id']}/live-sessions",
        json=_register_body(label="b", is_default=True),
        headers=auth_headers,
    )
    assert r2.status_code == 409


# ─── List ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_returns_only_active_by_default(client, auth_headers):
    agent = await _make_agent(client, auth_headers)
    await client.post(
        f"/v1/agents/{agent['id']}/live-sessions",
        json=_register_body(label="active"),
        headers=auth_headers,
    )
    await client.post(
        f"/v1/agents/{agent['id']}/live-sessions",
        json=_register_body(label="will-detach"),
        headers=auth_headers,
    )
    await client.delete(
        f"/v1/agents/{agent['id']}/live-sessions/will-detach",
        headers=auth_headers,
    )
    r = await client.get(
        f"/v1/agents/{agent['id']}/live-sessions",
        headers=auth_headers,
    )
    assert r.status_code == 200
    rows = r.json()
    labels = {row["label"] for row in rows}
    assert labels == {"active"}


@pytest.mark.asyncio
async def test_list_with_include_detached(client, auth_headers):
    agent = await _make_agent(client, auth_headers)
    await client.post(
        f"/v1/agents/{agent['id']}/live-sessions",
        json=_register_body(label="active"),
        headers=auth_headers,
    )
    await client.post(
        f"/v1/agents/{agent['id']}/live-sessions",
        json=_register_body(label="audit"),
        headers=auth_headers,
    )
    await client.delete(
        f"/v1/agents/{agent['id']}/live-sessions/audit",
        headers=auth_headers,
    )
    r = await client.get(
        f"/v1/agents/{agent['id']}/live-sessions?include_detached=true",
        headers=auth_headers,
    )
    assert r.status_code == 200
    rows = r.json()
    labels = {row["label"] for row in rows}
    assert labels == {"active", "audit"}
    audit = next(row for row in rows if row["label"] == "audit")
    assert audit["attached"] is False


# ─── Detach ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_detach_marks_inactive(client, auth_headers):
    agent = await _make_agent(client, auth_headers)
    await client.post(
        f"/v1/agents/{agent['id']}/live-sessions",
        json=_register_body(label="main"),
        headers=auth_headers,
    )
    r = await client.delete(
        f"/v1/agents/{agent['id']}/live-sessions/main",
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert r.json()["attached"] is False


@pytest.mark.asyncio
async def test_detach_unknown_label_404(client, auth_headers):
    agent = await _make_agent(client, auth_headers)
    r = await client.delete(
        f"/v1/agents/{agent['id']}/live-sessions/nonexistent",
        headers=auth_headers,
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "live_session_not_found"


# ─── Substrate Q5d — re-attach-after-detach pins ────────────────────


@pytest.mark.asyncio
async def test_relabel_after_detach(client, auth_headers):
    """After detach, a NEW session can register with the SAME label.

    Critical for label-reuse semantics across session-restarts (the
    canonical Live-attach pattern: bash Monitor dies → new bash
    Monitor attaches with same label). Per CWS Item 6 + 8 lock.
    """
    agent = await _make_agent(client, auth_headers)
    # Attach session A with label="main".
    r1 = await client.post(
        f"/v1/agents/{agent['id']}/live-sessions",
        json=_register_body(label="main"),
        headers=auth_headers,
    )
    assert r1.status_code == 201
    # Detach.
    rd = await client.delete(
        f"/v1/agents/{agent['id']}/live-sessions/main",
        headers=auth_headers,
    )
    assert rd.status_code == 200
    # Attach session B with the SAME label="main"; partial unique
    # index only constrains active rows so this should succeed.
    r2 = await client.post(
        f"/v1/agents/{agent['id']}/live-sessions",
        json=_register_body(label="main"),
        headers=auth_headers,
    )
    assert r2.status_code == 201, r2.text
    # Both rows visible with include_detached=true; only one active.
    r_list = await client.get(
        f"/v1/agents/{agent['id']}/live-sessions?include_detached=true",
        headers=auth_headers,
    )
    rows = r_list.json()
    assert len(rows) == 2
    main_rows = [row for row in rows if row["label"] == "main"]
    assert len(main_rows) == 2
    attached_count = sum(1 for row in main_rows if row["attached"])
    assert attached_count == 1


@pytest.mark.asyncio
async def test_redefault_after_detach(client, auth_headers):
    """After detaching the default session, a new register with
    is_default=true succeeds even though the audit-trail row had
    is_default=true at attach time.

    The partial unique index uses ``WHERE is_default = true AND
    detached_at IS NULL`` — soft-detached rows don't block the
    "at most one default" constraint.
    """
    agent = await _make_agent(client, auth_headers)
    # Attach default session A.
    r1 = await client.post(
        f"/v1/agents/{agent['id']}/live-sessions",
        json=_register_body(label="primary", is_default=True),
        headers=auth_headers,
    )
    assert r1.status_code == 201
    # Detach.
    rd = await client.delete(
        f"/v1/agents/{agent['id']}/live-sessions/primary",
        headers=auth_headers,
    )
    assert rd.status_code == 200
    # Fresh register with is_default=true — should succeed.
    r2 = await client.post(
        f"/v1/agents/{agent['id']}/live-sessions",
        json=_register_body(label="primary", is_default=True),
        headers=auth_headers,
    )
    assert r2.status_code == 201, r2.text
    assert r2.json()["is_default"] is True


# ─── Patch ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_set_default_atomic_flip(client, auth_headers):
    agent = await _make_agent(client, auth_headers)
    # Two sessions; A is default.
    await client.post(
        f"/v1/agents/{agent['id']}/live-sessions",
        json=_register_body(label="a", is_default=True),
        headers=auth_headers,
    )
    await client.post(
        f"/v1/agents/{agent['id']}/live-sessions",
        json=_register_body(label="b", is_default=False),
        headers=auth_headers,
    )
    # Flip B to default; A should auto-flip to false.
    r = await client.patch(
        f"/v1/agents/{agent['id']}/live-sessions/b",
        json={"is_default": True},
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert r.json()["is_default"] is True
    # Verify A is no longer default.
    rl = await client.get(
        f"/v1/agents/{agent['id']}/live-sessions",
        headers=auth_headers,
    )
    rows = rl.json()
    a_row = next(row for row in rows if row["label"] == "a")
    b_row = next(row for row in rows if row["label"] == "b")
    assert a_row["is_default"] is False
    assert b_row["is_default"] is True


@pytest.mark.asyncio
async def test_patch_session_token_rotates(client, auth_headers):
    agent = await _make_agent(client, auth_headers)
    await client.post(
        f"/v1/agents/{agent['id']}/live-sessions",
        json=_register_body(label="main", session_token="ULID-OLD"),
        headers=auth_headers,
    )
    new_token = "01HZWC4KGE7ZYAZQX8JBQK9MPN"
    r = await client.patch(
        f"/v1/agents/{agent['id']}/live-sessions/main",
        json={"session_token": new_token},
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert r.json()["session_token"] == new_token


@pytest.mark.asyncio
async def test_patch_empty_body_400(client, auth_headers):
    agent = await _make_agent(client, auth_headers)
    await client.post(
        f"/v1/agents/{agent['id']}/live-sessions",
        json=_register_body(label="main"),
        headers=auth_headers,
    )
    r = await client.patch(
        f"/v1/agents/{agent['id']}/live-sessions/main",
        json={},
        headers=auth_headers,
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "no_mutable_fields"


@pytest.mark.asyncio
async def test_patch_is_default_false_alone_400(client, auth_headers):
    agent = await _make_agent(client, auth_headers)
    await client.post(
        f"/v1/agents/{agent['id']}/live-sessions",
        json=_register_body(label="main", is_default=True),
        headers=auth_headers,
    )
    r = await client.patch(
        f"/v1/agents/{agent['id']}/live-sessions/main",
        json={"is_default": False},
        headers=auth_headers,
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_default_flip"


@pytest.mark.asyncio
async def test_patch_unknown_label_404(client, auth_headers):
    agent = await _make_agent(client, auth_headers)
    r = await client.patch(
        f"/v1/agents/{agent['id']}/live-sessions/nonexistent",
        json={"is_default": True},
        headers=auth_headers,
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "live_session_not_found"


# ─── Cross-user isolation ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_register_cross_user_returns_404(
    client, auth_headers, other_auth_headers
):
    """Agent owned by user B; user A's auth header → register returns 404
    (not 403; doesn't leak existence)."""
    agent = await _make_agent(client, other_auth_headers)
    r = await client.post(
        f"/v1/agents/{agent['id']}/live-sessions",
        json=_register_body(),
        headers=auth_headers,
    )
    assert r.status_code == 404
