"""POST /v1/executions/{id}/live-claim cmotigtnx attestation tests.

Records ``live_claim_session_token`` + ``live_claimed_at`` on the
execution row. Write-once semantics: same token = idempotent 200;
different token after first attestation = 409 already_attested.

Direct service-layer tests (`_resolve_live_claim_attestation`)
exercise each branch for patch coverage; pytest-cov on Python 3.12
doesn't trace ASGI dispatch through HTTP integration tests
reliably (per CLAUDE.md docs).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app.models.cue import Cue
from app.models.execution import Execution
from app.routers.executions import _resolve_live_claim_attestation
from app.utils.ids import generate_cue_id


@pytest_asyncio.fixture
async def cue_with_pending_execution(client, registered_user, db_session):
    """Create an authed cue + execution suitable for live-claim attestation."""
    from sqlalchemy import select
    from app.models.user import User
    from app.utils.ids import hash_api_key

    api_key_hash = hash_api_key(registered_user["api_key"])
    result = await db_session.execute(select(User).where(User.api_key_hash == api_key_hash))
    user = result.scalar_one()

    cue_id = generate_cue_id()
    cue = Cue(
        id=cue_id,
        user_id=str(user.id),
        name="live-claim-test",
        status="active",
        schedule_type="recurring",
        schedule_cron="0 9 * * *",
        schedule_timezone="UTC",
        callback_url="https://example.com/hook",
        callback_method="POST",
        callback_headers={},
        payload={"task": "live-test"},
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
        status="pending",
        attempts=0,
    )
    db_session.add(execution)
    await db_session.commit()

    return {
        "cue_id": cue_id,
        "execution_id": str(exec_id),
        "auth_headers": {"Authorization": f"Bearer {registered_user['api_key']}"},
    }


# ─── HTTP integration ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_live_claim_fresh_attestation(client, cue_with_pending_execution):
    data = cue_with_pending_execution
    r = await client.post(
        f"/v1/executions/{data['execution_id']}/live-claim",
        json={"session_token": "01HZWC4KGE7ZYAZQX8JBQK9MPN"},
        headers=data["auth_headers"],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["attested"] is True
    assert body["execution_id"] == data["execution_id"]
    assert body["live_claimed_at"] is not None


@pytest.mark.asyncio
async def test_live_claim_idempotent_same_token(client, cue_with_pending_execution):
    data = cue_with_pending_execution
    token = "01HZWC4KGE7ZYAZQX8JBQK9MPN"
    r1 = await client.post(
        f"/v1/executions/{data['execution_id']}/live-claim",
        json={"session_token": token},
        headers=data["auth_headers"],
    )
    assert r1.status_code == 200
    first_claimed_at = r1.json()["live_claimed_at"]

    # Same token, second call → idempotent 200 with the SAME timestamp.
    r2 = await client.post(
        f"/v1/executions/{data['execution_id']}/live-claim",
        json={"session_token": token},
        headers=data["auth_headers"],
    )
    assert r2.status_code == 200
    assert r2.json()["live_claimed_at"] == first_claimed_at


@pytest.mark.asyncio
async def test_live_claim_conflict_different_token(client, cue_with_pending_execution):
    data = cue_with_pending_execution
    r1 = await client.post(
        f"/v1/executions/{data['execution_id']}/live-claim",
        json={"session_token": "01HZWC4KGE7ZYAZQX8JBQK9MPN"},
        headers=data["auth_headers"],
    )
    assert r1.status_code == 200

    # Different token after first attestation → 409 already_attested.
    r2 = await client.post(
        f"/v1/executions/{data['execution_id']}/live-claim",
        json={"session_token": "01HZWCDIFFERENT00000000000"},
        headers=data["auth_headers"],
    )
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "already_attested"


@pytest.mark.asyncio
async def test_live_claim_unknown_execution_404(client, auth_headers):
    fake_id = str(uuid.uuid4())
    r = await client.post(
        f"/v1/executions/{fake_id}/live-claim",
        json={"session_token": "01HZWC4KGE7ZYAZQX8JBQK9MPN"},
        headers=auth_headers,
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "execution_not_found"


@pytest.mark.asyncio
async def test_live_claim_cross_user_404(
    client, cue_with_pending_execution, other_auth_headers
):
    """User A's execution; user B's auth → 404 (not 403; no leak)."""
    data = cue_with_pending_execution
    r = await client.post(
        f"/v1/executions/{data['execution_id']}/live-claim",
        json={"session_token": "01HZWC4KGE7ZYAZQX8JBQK9MPN"},
        headers=other_auth_headers,
    )
    assert r.status_code == 404


# ─── Direct service-layer tests (patch-coverage path) ──────────────


class _FakeExecution:
    """Minimal stand-in for Execution rows in pure-helper tests."""

    def __init__(self, *, id_: str, live_claimed_at=None, live_claim_session_token=None):
        self.id = id_
        self.live_claimed_at = live_claimed_at
        self.live_claim_session_token = live_claim_session_token


def test_resolve_attestation_fresh_writes_token():
    exec_ = _FakeExecution(id_="exec-1")
    now = datetime.now(timezone.utc)
    outcome, payload = _resolve_live_claim_attestation(
        execution=exec_,
        session_token="01HZWC4KGE7ZYAZQX8JBQK9MPN",
        now=now,
    )
    assert outcome == "fresh"
    assert exec_.live_claim_session_token == "01HZWC4KGE7ZYAZQX8JBQK9MPN"
    assert exec_.live_claimed_at == now
    assert payload["attested"] is True
    assert payload["execution_id"] == "exec-1"
    assert payload["live_claimed_at"] == now


def test_resolve_attestation_idempotent_same_token():
    prior = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    exec_ = _FakeExecution(
        id_="exec-2",
        live_claimed_at=prior,
        live_claim_session_token="01HZWC4KGE7ZYAZQX8JBQK9MPN",
    )
    now = datetime.now(timezone.utc)
    outcome, payload = _resolve_live_claim_attestation(
        execution=exec_,
        session_token="01HZWC4KGE7ZYAZQX8JBQK9MPN",
        now=now,
    )
    assert outcome == "idempotent"
    # Existing attestation NOT overwritten.
    assert exec_.live_claimed_at == prior
    assert payload["live_claimed_at"] == prior


def test_resolve_attestation_conflict_different_token():
    prior = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    exec_ = _FakeExecution(
        id_="exec-3",
        live_claimed_at=prior,
        live_claim_session_token="01HZWC4KGE7ZYAZQX8JBQK9MPN",
    )
    now = datetime.now(timezone.utc)
    outcome, payload = _resolve_live_claim_attestation(
        execution=exec_,
        session_token="01HZWCOTHERULID000000000000",
        now=now,
    )
    assert outcome == "conflict"
    assert payload["error"]["code"] == "already_attested"
    assert payload["error"]["status"] == 409
    # Prior attestation NOT overwritten by conflict caller.
    assert exec_.live_claim_session_token == "01HZWC4KGE7ZYAZQX8JBQK9MPN"
    assert exec_.live_claimed_at == prior
