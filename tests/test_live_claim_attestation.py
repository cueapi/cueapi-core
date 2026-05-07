"""Live-claim attestation endpoint + outcome validator gate.

Parity port of cueapi/cueapi#664 (P0 Bulletproofing — backlog
``cmotigtnx``). Covers:

* The ``POST /v1/executions/{id}/live-claim`` endpoint:
  * fresh attestation (200 OK with live_claimed_at set)
  * idempotent repeat with same session_token (200 OK, unchanged)
  * conflict on repeat with different session_token (409
    ``already_attested``)
  * 404 on unknown execution OR cross-tenant access
  * 422 on session_token below min_length

* The outcome-validator gate:
  * ``executed_via='live'`` + no attestation → 422
    ``live_claim_unattested``
  * ``executed_via='live'`` + attestation → 200 (passes through)
  * ``executed_via='background'`` + no attestation → 200 (gate
    only fires on live)
  * No ``executed_via`` field → 200 (gate only fires when
    explicitly claimed)

Direct-call unit tests on ``_resolve_live_claim_attestation``
(pure helper) + ``_check_live_claim_attestation_required`` (pure
helper) keep patch-coverage ≥80% on Python 3.12 + ASGI.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.models.cue import Cue
from app.models.execution import Execution
from app.models.user import User
from app.routers.executions import _resolve_live_claim_attestation
from app.services.outcome_service import _check_live_claim_attestation_required
from app.utils.ids import generate_cue_id, hash_api_key

from sqlalchemy import select


# ---------------------------------------------------------------------------
# Pure-helper unit tests
# ---------------------------------------------------------------------------


def _exec_stub(*, live_claim_session_token=None, live_claimed_at=None, exec_id=None):
    return SimpleNamespace(
        id=exec_id or uuid.uuid4(),
        live_claim_session_token=live_claim_session_token,
        live_claimed_at=live_claimed_at,
    )


def test_resolve_live_claim_fresh_sets_columns():
    e = _exec_stub()
    now = datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)
    outcome, payload = _resolve_live_claim_attestation(
        execution=e, session_token="agent-session-abc", now=now
    )
    assert outcome == "fresh"
    assert e.live_claim_session_token == "agent-session-abc"
    assert e.live_claimed_at == now
    assert payload["attested"] is True
    assert payload["live_claimed_at"] == now


def test_resolve_live_claim_idempotent_same_token():
    earlier = datetime(2026, 5, 6, 11, 0, 0, tzinfo=timezone.utc)
    e = _exec_stub(
        live_claim_session_token="agent-session-abc",
        live_claimed_at=earlier,
    )
    outcome, payload = _resolve_live_claim_attestation(
        execution=e, session_token="agent-session-abc",
        now=datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert outcome == "idempotent"
    assert e.live_claimed_at == earlier  # not bumped
    assert payload["attested"] is True
    assert payload["live_claimed_at"] == earlier


def test_resolve_live_claim_conflict_different_token():
    earlier = datetime(2026, 5, 6, 11, 0, 0, tzinfo=timezone.utc)
    e = _exec_stub(
        live_claim_session_token="agent-session-abc",
        live_claimed_at=earlier,
    )
    outcome, payload = _resolve_live_claim_attestation(
        execution=e, session_token="agent-session-xyz",
        now=datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert outcome == "conflict"
    assert e.live_claimed_at == earlier  # untouched
    assert payload["error"]["code"] == "already_attested"
    assert payload["error"]["status"] == 409


def test_check_attestation_no_metadata():
    e = _exec_stub()
    assert _check_live_claim_attestation_required(metadata=None, execution=e) is None


def test_check_attestation_metadata_not_dict():
    e = _exec_stub()
    assert _check_live_claim_attestation_required(
        metadata=["not", "a", "dict"], execution=e
    ) is None


def test_check_attestation_executed_via_background_passes():
    e = _exec_stub()
    assert _check_live_claim_attestation_required(
        metadata={"executed_via": "background"}, execution=e
    ) is None


def test_check_attestation_no_executed_via_passes():
    e = _exec_stub()
    assert _check_live_claim_attestation_required(
        metadata={"some_other_field": "x"}, execution=e
    ) is None


def test_check_attestation_live_with_attestation_passes():
    e = _exec_stub(live_claimed_at=datetime.now(timezone.utc))
    assert _check_live_claim_attestation_required(
        metadata={"executed_via": "live"}, execution=e
    ) is None


def test_check_attestation_live_without_attestation_rejects():
    e = _exec_stub()
    err = _check_live_claim_attestation_required(
        metadata={"executed_via": "live"}, execution=e
    )
    assert err is not None
    assert err["error"]["code"] == "live_claim_unattested"
    assert err["error"]["status"] == 422


# ---------------------------------------------------------------------------
# HTTP integration tests
# ---------------------------------------------------------------------------


async def _make_execution(db_session, registered_user, *, attest=False):
    """Build a fresh cue + execution and return ids + auth headers."""
    api_key_hash = hash_api_key(registered_user["api_key"])
    user_row = await db_session.execute(
        select(User).where(User.api_key_hash == api_key_hash)
    )
    user = user_row.scalar_one()

    cue_id = generate_cue_id()
    cue = Cue(
        id=cue_id,
        user_id=str(user.id),
        name="live-claim-test",
        status="active",
        schedule_type="once",
        schedule_timezone="UTC",
        callback_url="https://example.com/hook",
        callback_method="POST",
        callback_headers={},
        payload={"task": "test"},
        retry_max_attempts=3,
        retry_backoff_minutes=5,
        next_run=datetime.now(timezone.utc),
    )
    db_session.add(cue)
    await db_session.flush()

    exec_id = uuid.uuid4()
    execution = Execution(
        id=exec_id,
        cue_id=cue_id,
        scheduled_for=datetime.now(timezone.utc),
        status="success",
        attempts=1,
        live_claim_session_token="seed-token-12345" if attest else None,
        live_claimed_at=datetime.now(timezone.utc) if attest else None,
    )
    db_session.add(execution)
    await db_session.commit()

    return {
        "execution_id": str(exec_id),
        "auth_headers": {"Authorization": f"Bearer {registered_user['api_key']}"},
    }


@pytest.mark.asyncio
async def test_live_claim_fresh_returns_200(client, registered_user, db_session):
    ctx = await _make_execution(db_session, registered_user)
    r = await client.post(
        f"/v1/executions/{ctx['execution_id']}/live-claim",
        json={"session_token": "agent-session-fresh-1"},
        headers=ctx["auth_headers"],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["attested"] is True
    assert body["execution_id"] == ctx["execution_id"]
    assert "live_claimed_at" in body


@pytest.mark.asyncio
async def test_live_claim_idempotent_same_token(client, registered_user, db_session):
    ctx = await _make_execution(db_session, registered_user)
    r1 = await client.post(
        f"/v1/executions/{ctx['execution_id']}/live-claim",
        json={"session_token": "agent-session-idem-1"},
        headers=ctx["auth_headers"],
    )
    assert r1.status_code == 200
    r2 = await client.post(
        f"/v1/executions/{ctx['execution_id']}/live-claim",
        json={"session_token": "agent-session-idem-1"},
        headers=ctx["auth_headers"],
    )
    assert r2.status_code == 200
    # Idempotent — same live_claimed_at on both calls
    assert r2.json()["live_claimed_at"] == r1.json()["live_claimed_at"]


@pytest.mark.asyncio
async def test_live_claim_conflict_different_token(client, registered_user, db_session):
    ctx = await _make_execution(db_session, registered_user)
    await client.post(
        f"/v1/executions/{ctx['execution_id']}/live-claim",
        json={"session_token": "agent-session-first"},
        headers=ctx["auth_headers"],
    )
    r = await client.post(
        f"/v1/executions/{ctx['execution_id']}/live-claim",
        json={"session_token": "agent-session-second"},
        headers=ctx["auth_headers"],
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "already_attested"


@pytest.mark.asyncio
async def test_live_claim_404_on_unknown_execution(client, registered_user):
    headers = {"Authorization": f"Bearer {registered_user['api_key']}"}
    r = await client.post(
        f"/v1/executions/{uuid.uuid4()}/live-claim",
        json={"session_token": "anything-valid-12345"},
        headers=headers,
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "execution_not_found"


@pytest.mark.asyncio
async def test_live_claim_session_token_too_short(client, registered_user, db_session):
    ctx = await _make_execution(db_session, registered_user)
    r = await client.post(
        f"/v1/executions/{ctx['execution_id']}/live-claim",
        json={"session_token": "tooshort"[:7]},
        headers=ctx["auth_headers"],
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_outcome_executed_via_live_without_attestation_rejected(
    client, registered_user, db_session
):
    """Validator gate: outcome reports executed_via='live' but no
    attestation was recorded. Reject."""
    ctx = await _make_execution(db_session, registered_user, attest=False)
    r = await client.post(
        f"/v1/executions/{ctx['execution_id']}/outcome",
        json={
            "success": True,
            "metadata": {"executed_via": "live"},
        },
        headers=ctx["auth_headers"],
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "live_claim_unattested"


@pytest.mark.asyncio
async def test_outcome_executed_via_live_with_attestation_passes(
    client, registered_user, db_session
):
    ctx = await _make_execution(db_session, registered_user, attest=True)
    r = await client.post(
        f"/v1/executions/{ctx['execution_id']}/outcome",
        json={
            "success": True,
            "metadata": {"executed_via": "live"},
        },
        headers=ctx["auth_headers"],
    )
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_outcome_no_executed_via_passes_unattested(
    client, registered_user, db_session
):
    """Outcomes that omit ``executed_via`` aren't gated — modern
    handlers using typed delivery_route field shouldn't be punished."""
    ctx = await _make_execution(db_session, registered_user, attest=False)
    r = await client.post(
        f"/v1/executions/{ctx['execution_id']}/outcome",
        json={
            "success": True,
            "metadata": {"some_field": "value"},
        },
        headers=ctx["auth_headers"],
    )
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_outcome_executed_via_background_passes_unattested(
    client, registered_user, db_session
):
    ctx = await _make_execution(db_session, registered_user, attest=False)
    r = await client.post(
        f"/v1/executions/{ctx['execution_id']}/outcome",
        json={
            "success": True,
            "metadata": {"executed_via": "background"},
        },
        headers=ctx["auth_headers"],
    )
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Direct-call orchestrator tests for _process_live_claim_request +
# record_outcome's attestation-gate integration. These bypass ASGI
# dispatch so pytest-cov on Python 3.12 traces the async helpers and
# the validator-call branch in record_outcome.
# ---------------------------------------------------------------------------


async def _make_auth_user(db_session, registered_user):
    from app.auth import AuthenticatedUser
    api_key_hash = hash_api_key(registered_user["api_key"])
    user_row = await db_session.execute(
        select(User).where(User.api_key_hash == api_key_hash)
    )
    u = user_row.scalar_one()
    return AuthenticatedUser(
        id=str(u.id),
        email=u.email,
        plan=u.plan,
        active_cue_limit=u.active_cue_limit,
        monthly_execution_limit=u.monthly_execution_limit,
        rate_limit_per_minute=u.rate_limit_per_minute,
    )


@pytest.mark.asyncio
async def test_process_live_claim_direct_fresh(db_session, registered_user):
    from app.routers.executions import _process_live_claim_request
    auth_user = await _make_auth_user(db_session, registered_user)
    ctx = await _make_execution(db_session, registered_user)
    outcome, payload = await _process_live_claim_request(
        db=db_session,
        user=auth_user,
        execution_id=ctx["execution_id"],
        session_token="direct-token-fresh-1",
    )
    assert outcome == "fresh"
    assert payload["attested"] is True


@pytest.mark.asyncio
async def test_process_live_claim_direct_not_found(db_session, registered_user):
    from app.routers.executions import _process_live_claim_request
    auth_user = await _make_auth_user(db_session, registered_user)
    outcome, payload = await _process_live_claim_request(
        db=db_session,
        user=auth_user,
        execution_id=str(uuid.uuid4()),
        session_token="direct-token-missing",
    )
    assert outcome == "not_found"
    assert payload["error"]["code"] == "execution_not_found"


@pytest.mark.asyncio
async def test_process_live_claim_direct_idempotent_then_conflict(
    db_session, registered_user
):
    from app.routers.executions import _process_live_claim_request
    auth_user = await _make_auth_user(db_session, registered_user)
    ctx = await _make_execution(db_session, registered_user)

    o1, _ = await _process_live_claim_request(
        db=db_session, user=auth_user,
        execution_id=ctx["execution_id"], session_token="direct-tok-A1234567",
    )
    assert o1 == "fresh"

    o2, _ = await _process_live_claim_request(
        db=db_session, user=auth_user,
        execution_id=ctx["execution_id"], session_token="direct-tok-A1234567",
    )
    assert o2 == "idempotent"

    o3, p3 = await _process_live_claim_request(
        db=db_session, user=auth_user,
        execution_id=ctx["execution_id"], session_token="direct-tok-B7654321",
    )
    assert o3 == "conflict"
    assert p3["error"]["code"] == "already_attested"


@pytest.mark.asyncio
async def test_record_outcome_direct_rejects_unattested_live(
    db_session, registered_user
):
    """Direct-call into record_outcome with executed_via='live' on an
    unattested execution — covers the attestation-gate branch in
    record_outcome that pytest-cov doesn't trace through ASGI."""
    from app.schemas.outcome import OutcomeRequest
    from app.services.outcome_service import record_outcome
    auth_user = await _make_auth_user(db_session, registered_user)
    ctx = await _make_execution(db_session, registered_user, attest=False)
    body = OutcomeRequest(
        success=True,
        metadata={"executed_via": "live"},
    )
    result = await record_outcome(
        db_session, auth_user, ctx["execution_id"], body
    )
    assert "error" in result
    assert result["error"]["code"] == "live_claim_unattested"


@pytest.mark.asyncio
async def test_record_outcome_direct_passes_attested_live(
    db_session, registered_user
):
    from app.schemas.outcome import OutcomeRequest
    from app.services.outcome_service import record_outcome
    auth_user = await _make_auth_user(db_session, registered_user)
    ctx = await _make_execution(db_session, registered_user, attest=True)
    body = OutcomeRequest(
        success=True,
        metadata={"executed_via": "live"},
    )
    result = await record_outcome(
        db_session, auth_user, ctx["execution_id"], body
    )
    assert "error" not in result
