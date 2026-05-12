"""Item B Phase 1 — substrate tests for IPC attachment endpoints + service layer
+ ASYNC fire-accept dispatcher.

Live-delivery-v3 substrate primitive. Joint design lock at
https://trydock.ai/mike/live-delivery-v3-build-hub. Mike Q-B ratify
2026-05-12 ~00:38Z: ASYNC fire-accept dispatcher path.

Coverage targets:

- Service layer: ``create_attachment`` (3 branches: created / same-daemon
  supersede / cross-daemon conflict), ``delete_attachment`` (idempotent /
  delete), ``reconcile_attachments`` (UPSERT + downgrade-unmentioned).
- Router endpoints: POST /attachments (201 + 409), DELETE (204 + 200),
  POST /reconcile-attachments (200 + 400 mismatch).
- Daemon-id header: missing / malformed → 400.
- Token-format validation: app-layer ULID regex on AttachmentCreate.
- ASYNC dispatcher: _build_ipc_delivery_metadata helper (returns dict on
  match, None otherwise).
- Backwards-compat: existing webhook + worker-transport cues unchanged
  (no outcome_metadata stamp).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.agent import Agent
from app.models.agent_live_session import AgentLiveSession


DAEMON_ID_HEADER = "X-CueAPI-Daemon-Id"


# ───────────────────────────────────────────────────────────────────────
# Helper fixtures + utilities
# ───────────────────────────────────────────────────────────────────────


async def _make_agent(client, auth_headers, slug=None):
    payload = {"display_name": f"Agent {uuid.uuid4().hex[:6]}"}
    if slug:
        payload["slug"] = slug
    r = await client.post("/v1/agents", json=payload, headers=auth_headers)
    assert r.status_code == 201, r.text
    return r.json()


def _make_ulid() -> str:
    """Generate a valid 26-char ULID-shaped token for testing."""
    # Crockford base32 alphabet (no I/L/O/U); just need a regex-passing string.
    return "01ABCDEFGHJKMNPQRSTV" + uuid.uuid4().hex[:6].upper().replace("I", "J").replace("L", "M").replace("O", "P").replace("U", "V")


def _daemon_headers(daemon_id: str | None = None) -> dict:
    return {DAEMON_ID_HEADER: daemon_id or str(uuid.uuid4())}


# ───────────────────────────────────────────────────────────────────────
# Helper unit tests — _parse_daemon_id, _build_ipc_delivery_metadata
# ───────────────────────────────────────────────────────────────────────


def test_parse_daemon_id_valid_uuid():
    from app.routers.ipc_attachments import _parse_daemon_id
    valid = str(uuid.uuid4())
    parsed, err = _parse_daemon_id(valid)
    assert err is None
    assert str(parsed) == valid


def test_parse_daemon_id_missing_returns_400():
    from app.routers.ipc_attachments import _parse_daemon_id
    parsed, err = _parse_daemon_id(None)
    assert parsed is None
    assert err is not None
    assert err.status_code == 400


def test_parse_daemon_id_empty_string_returns_400():
    from app.routers.ipc_attachments import _parse_daemon_id
    parsed, err = _parse_daemon_id("")
    assert parsed is None
    assert err is not None and err.status_code == 400


def test_parse_daemon_id_malformed_returns_400():
    from app.routers.ipc_attachments import _parse_daemon_id
    parsed, err = _parse_daemon_id("not-a-uuid")
    assert parsed is None
    assert err is not None and err.status_code == 400


def test_parse_daemon_id_whitespace_stripped():
    from app.routers.ipc_attachments import _parse_daemon_id
    valid = str(uuid.uuid4())
    parsed, err = _parse_daemon_id(f"  {valid}  ")
    assert err is None
    assert str(parsed) == valid


@pytest.mark.asyncio
async def test_build_ipc_delivery_metadata_none_when_no_attachment(db_session):
    """Cue with no agent_live_sessions row → None (no metadata stamp)."""
    from app.routers.cues import _build_ipc_delivery_metadata
    result = await _build_ipc_delivery_metadata(db_session, "cue_nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_build_ipc_delivery_metadata_ipc_active_returns_dict(
    db_session, registered_user
):
    """Cue with an active IPC attachment → returns {delivery_mode_requested: ipc}."""
    from app.routers.cues import _build_ipc_delivery_metadata
    from app.utils.ids import generate_agent_id

    user = (
        await db_session.execute(
            select(__import__("app.models.user", fromlist=["User"]).User).where(
                __import__("app.models.user", fromlist=["User"]).User.email
                == registered_user["email"]
            )
        )
    ).scalar_one()

    agent = Agent(
        id=generate_agent_id(),
        user_id=user.id,
        slug=f"ipc-meta-{uuid.uuid4().hex[:6]}",
        display_name="IPC Meta Agent",
    )
    db_session.add(agent)
    await db_session.flush()
    sess = AgentLiveSession(
        agent_id=agent.id,
        label="main",
        cue_id="cue_ipctest12345",
        task_name="max-claude-code-test",
        attached_at=datetime.now(timezone.utc),
        ipc_session_token=_make_ulid(),
        transport="ipc",
        daemon_id=uuid.uuid4(),
        last_reconciled_at=datetime.now(timezone.utc),
    )
    db_session.add(sess)
    await db_session.commit()

    result = await _build_ipc_delivery_metadata(db_session, "cue_ipctest12345")
    assert result == {"delivery_mode_requested": "ipc"}


@pytest.mark.asyncio
async def test_build_ipc_delivery_metadata_poll_returns_none(
    db_session, registered_user
):
    """Cue with attachment on transport='poll' → None (not IPC)."""
    from app.routers.cues import _build_ipc_delivery_metadata
    from app.utils.ids import generate_agent_id

    user = (
        await db_session.execute(
            select(__import__("app.models.user", fromlist=["User"]).User).where(
                __import__("app.models.user", fromlist=["User"]).User.email
                == registered_user["email"]
            )
        )
    ).scalar_one()

    agent = Agent(
        id=generate_agent_id(),
        user_id=user.id,
        slug=f"ipc-poll-{uuid.uuid4().hex[:6]}",
        display_name="IPC Poll Agent",
    )
    db_session.add(agent)
    await db_session.flush()
    sess = AgentLiveSession(
        agent_id=agent.id,
        label="main",
        cue_id="cue_polltest12345",
        task_name="max-claude-code-test",
        attached_at=datetime.now(timezone.utc),
        transport="poll",  # default — not IPC
    )
    db_session.add(sess)
    await db_session.commit()

    result = await _build_ipc_delivery_metadata(db_session, "cue_polltest12345")
    assert result is None


@pytest.mark.asyncio
async def test_build_ipc_delivery_metadata_detached_returns_none(
    db_session, registered_user
):
    """Cue with IPC attachment but detached_at set → None (treated as gone)."""
    from app.routers.cues import _build_ipc_delivery_metadata
    from app.utils.ids import generate_agent_id

    user = (
        await db_session.execute(
            select(__import__("app.models.user", fromlist=["User"]).User).where(
                __import__("app.models.user", fromlist=["User"]).User.email
                == registered_user["email"]
            )
        )
    ).scalar_one()

    agent = Agent(
        id=generate_agent_id(),
        user_id=user.id,
        slug=f"ipc-det-{uuid.uuid4().hex[:6]}",
        display_name="IPC Detached Agent",
    )
    db_session.add(agent)
    await db_session.flush()
    sess = AgentLiveSession(
        agent_id=agent.id,
        label="main",
        cue_id="cue_detached12345",
        task_name="max-claude-code-test",
        attached_at=datetime.now(timezone.utc),
        detached_at=datetime.now(timezone.utc),  # soft-detached
        ipc_session_token=_make_ulid(),
        transport="ipc",
        daemon_id=uuid.uuid4(),
    )
    db_session.add(sess)
    await db_session.commit()

    result = await _build_ipc_delivery_metadata(db_session, "cue_detached12345")
    assert result is None


# ───────────────────────────────────────────────────────────────────────
# Endpoint integration — POST /v1/agents/{ref}/attachments
# ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_attachment_missing_daemon_header_400(client, auth_headers):
    agent = await _make_agent(client, auth_headers, slug=f"miss-{uuid.uuid4().hex[:6]}")
    r = await client.post(
        f"/v1/agents/{agent['id']}/attachments",
        json={
            "label": "main",
            "task_name": "max-claude-code-test",
            "ipc_session_token": _make_ulid(),
        },
        headers=auth_headers,  # no X-CueAPI-Daemon-Id
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "missing_daemon_id"


@pytest.mark.asyncio
async def test_post_attachment_malformed_daemon_header_400(client, auth_headers):
    agent = await _make_agent(client, auth_headers, slug=f"mal-{uuid.uuid4().hex[:6]}")
    r = await client.post(
        f"/v1/agents/{agent['id']}/attachments",
        json={
            "label": "main",
            "task_name": "max-claude-code-test",
            "ipc_session_token": _make_ulid(),
        },
        headers={**auth_headers, DAEMON_ID_HEADER: "not-a-uuid"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_daemon_id"


@pytest.mark.asyncio
async def test_post_attachment_agent_not_found_404(client, auth_headers):
    r = await client.post(
        "/v1/agents/agt_doesnotexist/attachments",
        json={
            "label": "main",
            "task_name": "max-claude-code-test",
            "ipc_session_token": _make_ulid(),
        },
        headers={**auth_headers, **_daemon_headers()},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "agent_not_found"


@pytest.mark.asyncio
async def test_post_attachment_slug_form_rejected_400(client, auth_headers):
    """Phase 1: only opaque agent_id; slug-form deferred."""
    r = await client.post(
        "/v1/agents/some-slug@user/attachments",
        json={
            "label": "main",
            "task_name": "max-claude-code-test",
            "ipc_session_token": _make_ulid(),
        },
        headers={**auth_headers, **_daemon_headers()},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_agent_ref"


@pytest.mark.asyncio
async def test_post_attachment_invalid_token_format_422(client, auth_headers):
    """App-layer ULID regex rejects bad token shapes at the Pydantic layer."""
    agent = await _make_agent(client, auth_headers, slug=f"bad-{uuid.uuid4().hex[:6]}")
    r = await client.post(
        f"/v1/agents/{agent['id']}/attachments",
        json={
            "label": "main",
            "task_name": "max-claude-code-test",
            "ipc_session_token": "not-a-ulid",  # < 26 chars
        },
        headers={**auth_headers, **_daemon_headers()},
    )
    # Pydantic rejects with 422 validation error (min_length=26)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_post_attachment_happy_201(client, auth_headers):
    agent = await _make_agent(client, auth_headers, slug=f"ok-{uuid.uuid4().hex[:6]}")
    daemon_id = str(uuid.uuid4())
    token = _make_ulid()
    r = await client.post(
        f"/v1/agents/{agent['id']}/attachments",
        json={
            "label": "main",
            "task_name": "max-claude-code-test",
            "ipc_session_token": token,
        },
        headers={**auth_headers, DAEMON_ID_HEADER: daemon_id},
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["agent_id"] == agent["id"]
    assert data["label"] == "main"
    assert data["transport"] == "ipc"
    assert data["ipc_session_token"] == token
    assert data["daemon_id"] == daemon_id
    assert data["supersedes_token"] is None


@pytest.mark.asyncio
async def test_post_attachment_same_daemon_supersede(client, auth_headers):
    """Same (agent, label, daemon_id) reattach: REPLACE; supersedes_token set."""
    agent = await _make_agent(client, auth_headers, slug=f"sup-{uuid.uuid4().hex[:6]}")
    daemon_id = str(uuid.uuid4())
    token1 = _make_ulid()
    r1 = await client.post(
        f"/v1/agents/{agent['id']}/attachments",
        json={
            "label": "main",
            "task_name": "max-claude-code-test",
            "ipc_session_token": token1,
        },
        headers={**auth_headers, DAEMON_ID_HEADER: daemon_id},
    )
    assert r1.status_code == 201

    token2 = _make_ulid()
    r2 = await client.post(
        f"/v1/agents/{agent['id']}/attachments",
        json={
            "label": "main",
            "task_name": "max-claude-code-test",
            "ipc_session_token": token2,
        },
        headers={**auth_headers, DAEMON_ID_HEADER: daemon_id},
    )
    assert r2.status_code == 201, r2.text
    data2 = r2.json()
    assert data2["ipc_session_token"] == token2
    assert data2["supersedes_token"] == token1


@pytest.mark.asyncio
async def test_post_attachment_cross_daemon_conflict_409(client, auth_headers):
    """Different daemon attempts same (agent, label): 409 with existing_daemon_id."""
    agent = await _make_agent(client, auth_headers, slug=f"x-{uuid.uuid4().hex[:6]}")
    daemon_a = str(uuid.uuid4())
    token_a = _make_ulid()
    r1 = await client.post(
        f"/v1/agents/{agent['id']}/attachments",
        json={
            "label": "main",
            "task_name": "max-claude-code-test",
            "ipc_session_token": token_a,
        },
        headers={**auth_headers, DAEMON_ID_HEADER: daemon_a},
    )
    assert r1.status_code == 201

    daemon_b = str(uuid.uuid4())
    r2 = await client.post(
        f"/v1/agents/{agent['id']}/attachments",
        json={
            "label": "main",
            "task_name": "max-claude-code-test",
            "ipc_session_token": _make_ulid(),
        },
        headers={**auth_headers, DAEMON_ID_HEADER: daemon_b},
    )
    assert r2.status_code == 409, r2.text
    err = r2.json()["error"]
    assert err["code"] == "attachment_exists"
    assert err["existing_daemon_id"] == daemon_a
    assert err["existing_token"] == token_a
    assert "existing_attached_at" in err


# ───────────────────────────────────────────────────────────────────────
# DELETE /v1/agents/{ref}/attachments/{token}
# ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_attachment_first_time_204(client, auth_headers):
    agent = await _make_agent(client, auth_headers, slug=f"d-{uuid.uuid4().hex[:6]}")
    daemon_id = str(uuid.uuid4())
    token = _make_ulid()
    await client.post(
        f"/v1/agents/{agent['id']}/attachments",
        json={"label": "main", "task_name": "x", "ipc_session_token": token},
        headers={**auth_headers, DAEMON_ID_HEADER: daemon_id},
    )
    r = await client.delete(
        f"/v1/agents/{agent['id']}/attachments/{token}",
        headers={**auth_headers, DAEMON_ID_HEADER: daemon_id},
    )
    assert r.status_code == 204


@pytest.mark.asyncio
async def test_delete_attachment_idempotent_200(client, auth_headers):
    """Second DELETE on same token: 200 with already_deleted reason."""
    agent = await _make_agent(client, auth_headers, slug=f"id-{uuid.uuid4().hex[:6]}")
    daemon_id = str(uuid.uuid4())
    token = _make_ulid()
    await client.post(
        f"/v1/agents/{agent['id']}/attachments",
        json={"label": "main", "task_name": "x", "ipc_session_token": token},
        headers={**auth_headers, DAEMON_ID_HEADER: daemon_id},
    )
    # First delete: 204
    await client.delete(
        f"/v1/agents/{agent['id']}/attachments/{token}",
        headers={**auth_headers, DAEMON_ID_HEADER: daemon_id},
    )
    # Second delete: 200 idempotent
    r = await client.delete(
        f"/v1/agents/{agent['id']}/attachments/{token}",
        headers={**auth_headers, DAEMON_ID_HEADER: daemon_id},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] is False
    assert body["reason"] == "already_deleted"


@pytest.mark.asyncio
async def test_delete_attachment_unknown_token_idempotent_200(client, auth_headers):
    """DELETE on a token that was never created: 200 idempotent (not 404)."""
    agent = await _make_agent(client, auth_headers, slug=f"u-{uuid.uuid4().hex[:6]}")
    r = await client.delete(
        f"/v1/agents/{agent['id']}/attachments/{_make_ulid()}",
        headers={**auth_headers, **_daemon_headers()},
    )
    assert r.status_code == 200
    assert r.json()["deleted"] is False


@pytest.mark.asyncio
async def test_delete_attachment_wrong_daemon_idempotent_200(client, auth_headers):
    """Daemon B trying to DELETE daemon A's token: scoped lookup misses → 200 idempotent.

    (Phase 1 design: daemon scoping prevents cross-daemon deletion by silently
    no-op-ing; daemon A's token stays alive. Daemon A's reconcile or explicit
    DELETE remains the path to revoke.)
    """
    agent = await _make_agent(client, auth_headers, slug=f"wd-{uuid.uuid4().hex[:6]}")
    daemon_a = str(uuid.uuid4())
    daemon_b = str(uuid.uuid4())
    token = _make_ulid()
    await client.post(
        f"/v1/agents/{agent['id']}/attachments",
        json={"label": "main", "task_name": "x", "ipc_session_token": token},
        headers={**auth_headers, DAEMON_ID_HEADER: daemon_a},
    )
    r = await client.delete(
        f"/v1/agents/{agent['id']}/attachments/{token}",
        headers={**auth_headers, DAEMON_ID_HEADER: daemon_b},
    )
    assert r.status_code == 200
    assert r.json()["deleted"] is False


# ───────────────────────────────────────────────────────────────────────
# POST /v1/agents/reconcile-attachments
# ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconcile_missing_daemon_header_400(client, auth_headers):
    r = await client.post(
        "/v1/agents/reconcile-attachments",
        json={
            "daemon_id": str(uuid.uuid4()),
            "reconciled_at": datetime.now(timezone.utc).isoformat(),
            "attachments": [],
        },
        headers=auth_headers,
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "missing_daemon_id"


@pytest.mark.asyncio
async def test_reconcile_header_body_mismatch_400(client, auth_headers):
    """Header daemon_id != body daemon_id → 400 daemon_id_mismatch."""
    header_id = str(uuid.uuid4())
    body_id = str(uuid.uuid4())
    r = await client.post(
        "/v1/agents/reconcile-attachments",
        json={
            "daemon_id": body_id,
            "reconciled_at": datetime.now(timezone.utc).isoformat(),
            "attachments": [],
        },
        headers={**auth_headers, DAEMON_ID_HEADER: header_id},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "daemon_id_mismatch"


@pytest.mark.asyncio
async def test_reconcile_empty_downgrades_all_daemons_rows(client, auth_headers):
    """Reconcile with empty attachments list downgrades all this daemon's IPC rows
    to transport='poll'."""
    agent = await _make_agent(client, auth_headers, slug=f"rc-{uuid.uuid4().hex[:6]}")
    daemon_id = str(uuid.uuid4())
    # Attach 2 sessions
    await client.post(
        f"/v1/agents/{agent['id']}/attachments",
        json={"label": "main", "task_name": "x", "ipc_session_token": _make_ulid()},
        headers={**auth_headers, DAEMON_ID_HEADER: daemon_id},
    )
    await client.post(
        f"/v1/agents/{agent['id']}/attachments",
        json={"label": "pr-watcher", "task_name": "y", "ipc_session_token": _make_ulid()},
        headers={**auth_headers, DAEMON_ID_HEADER: daemon_id},
    )

    # Reconcile with empty attachments — should downgrade both
    r = await client.post(
        "/v1/agents/reconcile-attachments",
        json={
            "daemon_id": daemon_id,
            "reconciled_at": datetime.now(timezone.utc).isoformat(),
            "attachments": [],
        },
        headers={**auth_headers, DAEMON_ID_HEADER: daemon_id},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["upserted_count"] == 0
    assert data["downgraded_count"] == 2


@pytest.mark.asyncio
async def test_reconcile_partial_upsert_and_downgrade(client, auth_headers):
    """Reconcile reporting 1 of 2 attachments: 1 upserted, 1 downgraded."""
    agent = await _make_agent(client, auth_headers, slug=f"rp-{uuid.uuid4().hex[:6]}")
    daemon_id = str(uuid.uuid4())
    token_main = _make_ulid()
    token_pr = _make_ulid()
    await client.post(
        f"/v1/agents/{agent['id']}/attachments",
        json={"label": "main", "task_name": "x", "ipc_session_token": token_main},
        headers={**auth_headers, DAEMON_ID_HEADER: daemon_id},
    )
    await client.post(
        f"/v1/agents/{agent['id']}/attachments",
        json={"label": "pr-watcher", "task_name": "y", "ipc_session_token": token_pr},
        headers={**auth_headers, DAEMON_ID_HEADER: daemon_id},
    )

    # Reconcile reports only "main" — "pr-watcher" should downgrade
    now = datetime.now(timezone.utc).isoformat()
    r = await client.post(
        "/v1/agents/reconcile-attachments",
        json={
            "daemon_id": daemon_id,
            "reconciled_at": now,
            "attachments": [
                {
                    "label": "main",
                    "task_name": "x",
                    "ipc_session_token": token_main,
                    "attached_at": now,
                }
            ],
        },
        headers={**auth_headers, DAEMON_ID_HEADER: daemon_id},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["upserted_count"] == 1
    assert data["downgraded_count"] == 1


@pytest.mark.asyncio
async def test_reconcile_does_not_affect_other_daemons_rows(client, auth_headers):
    """Daemon X reconcile does NOT touch daemon Y's rows (daemon-id scoping)."""
    agent_a = await _make_agent(client, auth_headers, slug=f"da-{uuid.uuid4().hex[:6]}")
    agent_b = await _make_agent(client, auth_headers, slug=f"db-{uuid.uuid4().hex[:6]}")
    daemon_x = str(uuid.uuid4())
    daemon_y = str(uuid.uuid4())
    await client.post(
        f"/v1/agents/{agent_a['id']}/attachments",
        json={"label": "main", "task_name": "x", "ipc_session_token": _make_ulid()},
        headers={**auth_headers, DAEMON_ID_HEADER: daemon_x},
    )
    await client.post(
        f"/v1/agents/{agent_b['id']}/attachments",
        json={"label": "main", "task_name": "y", "ipc_session_token": _make_ulid()},
        headers={**auth_headers, DAEMON_ID_HEADER: daemon_y},
    )

    # daemon_y reconciles with empty list → should downgrade daemon_y's row only
    r = await client.post(
        "/v1/agents/reconcile-attachments",
        json={
            "daemon_id": daemon_y,
            "reconciled_at": datetime.now(timezone.utc).isoformat(),
            "attachments": [],
        },
        headers={**auth_headers, DAEMON_ID_HEADER: daemon_y},
    )
    assert r.status_code == 200
    assert r.json()["downgraded_count"] == 1  # daemon_y's row only


# ───────────────────────────────────────────────────────────────────────
# Backwards-compat: existing webhook + worker-transport paths unchanged
# ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_backcompat_existing_agent_live_sessions_rows_default_to_poll(
    db_session, registered_user
):
    """Existing v2.x rows inserted without specifying transport inherit 'poll'."""
    from app.utils.ids import generate_agent_id

    user = (
        await db_session.execute(
            select(__import__("app.models.user", fromlist=["User"]).User).where(
                __import__("app.models.user", fromlist=["User"]).User.email
                == registered_user["email"]
            )
        )
    ).scalar_one()

    agent = Agent(
        id=generate_agent_id(),
        user_id=user.id,
        slug=f"bc-{uuid.uuid4().hex[:6]}",
        display_name="Backcompat Agent",
    )
    db_session.add(agent)
    await db_session.flush()
    # Insert WITHOUT specifying transport — should default to 'poll'
    sess = AgentLiveSession(
        agent_id=agent.id,
        label="main",
        cue_id=f"cue_bc{uuid.uuid4().hex[:6]}",
        task_name="max-claude-code-test",
        attached_at=datetime.now(timezone.utc),
    )
    db_session.add(sess)
    await db_session.commit()
    await db_session.refresh(sess)
    assert sess.transport == "poll"
    assert sess.daemon_id is None
    assert sess.ipc_session_token is None
    assert sess.last_reconciled_at is None


# ───────────────────────────────────────────────────────────────────────
# ASGI dispatch integration — exercises fire_cue's outcome_metadata=
# parameter line through real route + DB so pytest-cov on CI Py 3.11
# traces it. The pure helper _build_ipc_delivery_metadata is unit-tested
# above; this test pins the ASSIGNMENT line where the helper result
# flows into the Execution row.
# ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fire_cue_with_ipc_attachment_stamps_outcome_metadata(
    client, auth_headers, db_session
):
    """Fire a cue with an active IPC attachment → execution.outcome_metadata
    carries {"delivery_mode_requested": "ipc"}.

    This integration test exercises the ASGI-dispatched assignment line
    `outcome_metadata=ipc_outcome_metadata` in app/routers/cues.py:fire_cue.
    Pure helper is unit-tested above; this test ensures the wiring line
    actually flows through.
    """
    from app.models.execution import Execution
    from app.models.cue import Cue
    from app.utils.ids import generate_agent_id

    # Create a cue via the API (so it has the right shape + ownership)
    cue_resp = await client.post(
        "/v1/cues",
        json={
            "name": f"ipc-fire-{uuid.uuid4().hex[:6]}",
            "schedule": {"type": "recurring", "cron": "0 * * * *"},
            "transport": "worker",  # avoids webhook outbox creation noise
            "payload": {"task": "ipc-fire-test"},
        },
        headers=auth_headers,
    )
    assert cue_resp.status_code == 201, cue_resp.text
    cue_id = cue_resp.json()["id"]

    # Resolve the calling user + create an agent_live_session row that
    # matches the cue_id with transport='ipc'.
    cue_row = (
        await db_session.execute(select(Cue).where(Cue.id == cue_id))
    ).scalar_one()
    agent = Agent(
        id=generate_agent_id(),
        user_id=cue_row.user_id,
        slug=f"ipc-fire-{uuid.uuid4().hex[:6]}",
        display_name="IPC Fire Test Agent",
    )
    db_session.add(agent)
    await db_session.flush()
    sess = AgentLiveSession(
        agent_id=agent.id,
        label="main",
        cue_id=cue_id,  # match the cue
        task_name="max-claude-code-test",
        attached_at=datetime.now(timezone.utc),
        ipc_session_token=_make_ulid(),
        transport="ipc",
        daemon_id=uuid.uuid4(),
        last_reconciled_at=datetime.now(timezone.utc),
    )
    db_session.add(sess)
    await db_session.commit()

    # Fire the cue via the API
    fire_resp = await client.post(
        f"/v1/cues/{cue_id}/fire",
        headers=auth_headers,
    )
    assert fire_resp.status_code == 200, fire_resp.text
    execution_id = fire_resp.json()["id"]

    # Verify the execution's outcome_metadata carries the IPC stamp
    exec_row = (
        await db_session.execute(
            select(Execution).where(Execution.id == uuid.UUID(execution_id))
        )
    ).scalar_one()
    assert exec_row.outcome_metadata == {"delivery_mode_requested": "ipc"}


@pytest.mark.asyncio
async def test_fire_cue_without_ipc_attachment_leaves_outcome_metadata_null(
    client, auth_headers, db_session
):
    """Fire a cue with NO IPC attachment → execution.outcome_metadata is None.

    Companion to the IPC-stamped test above. Pins the None-branch of the
    helper-result flowing through fire_cue's Execution construction.
    """
    from app.models.execution import Execution

    cue_resp = await client.post(
        "/v1/cues",
        json={
            "name": f"no-ipc-{uuid.uuid4().hex[:6]}",
            "schedule": {"type": "recurring", "cron": "0 * * * *"},
            "transport": "worker",
            "payload": {"task": "no-ipc-test"},
        },
        headers=auth_headers,
    )
    assert cue_resp.status_code == 201, cue_resp.text
    cue_id = cue_resp.json()["id"]

    fire_resp = await client.post(
        f"/v1/cues/{cue_id}/fire",
        headers=auth_headers,
    )
    assert fire_resp.status_code == 200, fire_resp.text
    execution_id = fire_resp.json()["id"]

    exec_row = (
        await db_session.execute(
            select(Execution).where(Execution.id == uuid.UUID(execution_id))
        )
    ).scalar_one()
    assert exec_row.outcome_metadata is None


# ───────────────────────────────────────────────────────────────────────
# Service-layer direct unit tests (cover ASGI-dispatched service code
# bodies that pytest-cov on CI Py 3.11 misses; integration tests above
# exercise them at runtime but pytest-cov has known ASGI tracing gaps).
# ───────────────────────────────────────────────────────────────────────


async def _make_agent_row(db_session, registered_user, slug_suffix: str):
    """Test fixture: create + return an Agent row owned by registered_user."""
    from app.utils.ids import generate_agent_id
    from app.models.user import User

    user = (
        await db_session.execute(
            select(User).where(User.email == registered_user["email"])
        )
    ).scalar_one()
    agent = Agent(
        id=generate_agent_id(),
        user_id=user.id,
        slug=f"svc-{slug_suffix}-{uuid.uuid4().hex[:6]}",
        display_name=f"Svc Test {slug_suffix}",
    )
    db_session.add(agent)
    await db_session.flush()
    return agent


@pytest.mark.asyncio
async def test_service_create_attachment_happy_returns_created(db_session, registered_user):
    from app.services.ipc_attachment_service import create_attachment
    agent = await _make_agent_row(db_session, registered_user, "ca-h")
    token = _make_ulid()
    result = await create_attachment(
        db_session, agent_id=agent.id, label="main", task_name="t",
        ipc_session_token=token, daemon_id=uuid.uuid4(),
    )
    assert result.status == "created"
    assert result.row is not None
    assert result.row.ipc_session_token == token
    assert result.row.transport == "ipc"
    assert result.row.is_default is True
    assert result.supersedes_token is None


@pytest.mark.asyncio
async def test_service_create_attachment_non_main_label_not_default(db_session, registered_user):
    from app.services.ipc_attachment_service import create_attachment
    agent = await _make_agent_row(db_session, registered_user, "ca-n")
    result = await create_attachment(
        db_session, agent_id=agent.id, label="pr-watcher", task_name="t",
        ipc_session_token=_make_ulid(), daemon_id=uuid.uuid4(),
    )
    assert result.row is not None
    assert result.row.is_default is False


@pytest.mark.asyncio
async def test_service_create_attachment_same_daemon_supersedes(db_session, registered_user):
    from app.services.ipc_attachment_service import create_attachment
    agent = await _make_agent_row(db_session, registered_user, "ca-s")
    daemon_id = uuid.uuid4()
    token1 = _make_ulid()
    await create_attachment(
        db_session, agent_id=agent.id, label="main", task_name="t",
        ipc_session_token=token1, daemon_id=daemon_id,
    )
    token2 = _make_ulid()
    result = await create_attachment(
        db_session, agent_id=agent.id, label="main", task_name="t2",
        ipc_session_token=token2, daemon_id=daemon_id,
    )
    assert result.status == "created"
    assert result.row.ipc_session_token == token2
    assert result.supersedes_token == token1


@pytest.mark.asyncio
async def test_service_create_attachment_cross_daemon_conflict(db_session, registered_user):
    from app.services.ipc_attachment_service import create_attachment
    agent = await _make_agent_row(db_session, registered_user, "ca-x")
    daemon_a = uuid.uuid4()
    daemon_b = uuid.uuid4()
    token_a = _make_ulid()
    await create_attachment(
        db_session, agent_id=agent.id, label="main", task_name="t",
        ipc_session_token=token_a, daemon_id=daemon_a,
    )
    result = await create_attachment(
        db_session, agent_id=agent.id, label="main", task_name="t",
        ipc_session_token=_make_ulid(), daemon_id=daemon_b,
    )
    assert result.status == "conflict_cross_daemon"
    assert result.existing is not None
    assert result.existing.ipc_session_token == token_a
    assert result.existing.daemon_id == daemon_a
    assert result.row is None


@pytest.mark.asyncio
async def test_service_delete_attachment_deleted_branch(db_session, registered_user):
    from app.services.ipc_attachment_service import create_attachment, delete_attachment
    agent = await _make_agent_row(db_session, registered_user, "dl-d")
    daemon_id = uuid.uuid4()
    token = _make_ulid()
    await create_attachment(
        db_session, agent_id=agent.id, label="main", task_name="t",
        ipc_session_token=token, daemon_id=daemon_id,
    )
    result = await delete_attachment(
        db_session, agent_id=agent.id,
        ipc_session_token=token, daemon_id=daemon_id,
    )
    assert result.status == "deleted"


@pytest.mark.asyncio
async def test_service_delete_attachment_already_deleted_branch(db_session, registered_user):
    from app.services.ipc_attachment_service import delete_attachment
    agent = await _make_agent_row(db_session, registered_user, "dl-i")
    result = await delete_attachment(
        db_session, agent_id=agent.id,
        ipc_session_token=_make_ulid(), daemon_id=uuid.uuid4(),
    )
    assert result.status == "already_deleted"


@pytest.mark.asyncio
async def test_service_reconcile_empty_downgrades_all(db_session, registered_user):
    from app.services.ipc_attachment_service import create_attachment, reconcile_attachments
    agent = await _make_agent_row(db_session, registered_user, "rc-e")
    daemon_id = uuid.uuid4()
    await create_attachment(
        db_session, agent_id=agent.id, label="main", task_name="t",
        ipc_session_token=_make_ulid(), daemon_id=daemon_id,
    )
    await create_attachment(
        db_session, agent_id=agent.id, label="pr", task_name="t",
        ipc_session_token=_make_ulid(), daemon_id=daemon_id,
    )
    result = await reconcile_attachments(db_session, daemon_id=daemon_id, attachments=[])
    assert result.upserted_count == 0
    assert result.downgraded_count == 2


@pytest.mark.asyncio
async def test_service_reconcile_partial_upserts_and_downgrades(db_session, registered_user):
    from app.schemas.ipc_attachment import AttachmentReconcileEntry
    from app.services.ipc_attachment_service import create_attachment, reconcile_attachments
    agent = await _make_agent_row(db_session, registered_user, "rc-p")
    daemon_id = uuid.uuid4()
    token_a = _make_ulid()
    await create_attachment(
        db_session, agent_id=agent.id, label="main", task_name="t-main",
        ipc_session_token=token_a, daemon_id=daemon_id,
    )
    await create_attachment(
        db_session, agent_id=agent.id, label="pr", task_name="t-pr",
        ipc_session_token=_make_ulid(), daemon_id=daemon_id,
    )
    now = datetime.now(timezone.utc)
    result = await reconcile_attachments(
        db_session, daemon_id=daemon_id,
        attachments=[
            AttachmentReconcileEntry(
                label="main", task_name="t-main",
                ipc_session_token=token_a, attached_at=now,
            )
        ],
    )
    assert result.upserted_count == 1
    assert result.downgraded_count == 1


@pytest.mark.asyncio
async def test_service_reconcile_daemon_scoping(db_session, registered_user):
    from app.services.ipc_attachment_service import create_attachment, reconcile_attachments
    agent_a = await _make_agent_row(db_session, registered_user, "rc-da")
    agent_b = await _make_agent_row(db_session, registered_user, "rc-db")
    daemon_x = uuid.uuid4()
    daemon_y = uuid.uuid4()
    await create_attachment(
        db_session, agent_id=agent_a.id, label="main", task_name="t",
        ipc_session_token=_make_ulid(), daemon_id=daemon_x,
    )
    await create_attachment(
        db_session, agent_id=agent_b.id, label="main", task_name="t",
        ipc_session_token=_make_ulid(), daemon_id=daemon_y,
    )
    result = await reconcile_attachments(db_session, daemon_id=daemon_y, attachments=[])
    assert result.downgraded_count == 1


@pytest.mark.asyncio
async def test_service_reconcile_unknown_token_skipped(db_session, registered_user):
    from app.schemas.ipc_attachment import AttachmentReconcileEntry
    from app.services.ipc_attachment_service import reconcile_attachments
    now = datetime.now(timezone.utc)
    result = await reconcile_attachments(
        db_session, daemon_id=uuid.uuid4(),
        attachments=[
            AttachmentReconcileEntry(
                label="main", task_name="t",
                ipc_session_token=_make_ulid(), attached_at=now,
            )
        ],
    )
    assert result.upserted_count == 0
    assert result.downgraded_count == 0


# ───────────────────────────────────────────────────────────────────────
# Schema validator direct unit test (covers app/schemas/ipc_attachment.py:53)
# ───────────────────────────────────────────────────────────────────────


def test_schema_token_validator_rejects_malformed():
    from app.schemas.ipc_attachment import AttachmentCreate
    import pytest as _pytest
    with _pytest.raises(Exception):  # Pydantic ValidationError
        AttachmentCreate(
            label="main",
            task_name="t",
            ipc_session_token="this-is-not-a-ulid-shape!",
        )


def test_schema_token_validator_accepts_valid():
    from app.schemas.ipc_attachment import AttachmentCreate
    valid = "01ABCDEFGHJKMNPQRSTV" + "XYZ123"
    m = AttachmentCreate(label="main", task_name="t", ipc_session_token=valid)
    assert m.ipc_session_token == valid


def test_schema_token_validator_accepts_versioned_prefix():
    from app.schemas.ipc_attachment import AttachmentCreate
    valid = "v3a_01ABCDEFGHJKMNPQRSTVXYZ123"[:32]
    m = AttachmentCreate(label="main", task_name="t", ipc_session_token=valid)
    assert m.ipc_session_token == valid
