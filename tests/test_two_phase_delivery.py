"""Two-phase delivery + response-body outcomes + proactive alerting.

20 tests — ported from govindkavaturi-art/cueapi tests/test_two_phase_delivery.py

Status: ALL marked xfail — these features (delivery, alerts, _parse_outcome_from_response,
outcome_deadline_* columns) exist only in the hosted cueapi repo (commits 720c82e, b8a6345).
They have NOT been merged into cueapi-core yet.

These tests serve as a forward contract: they will automatically start passing
when the features land in cueapi-core, at which point the xfail markers should
be removed.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

HOSTED_ONLY = "hosted-only feature (720c82e/b8a6345) — not yet in cueapi-core"

from app.models.cue import Cue
from app.models.execution import Execution
from app.utils.ids import generate_cue_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_cue_payload(**overrides) -> dict:
    body = {
        "name": f"two-phase-{uuid.uuid4().hex[:6]}",
        "schedule": {"type": "once", "at": "2099-01-01T00:00:00Z", "timezone": "UTC"},
        "callback": {"url": "https://example.com/hook"},
        "payload": {"test": True},
    }
    body.update(overrides)
    return body


# ===========================================================================
# FIX 1 — Delivery & alerts config validation (7 tests)
# ===========================================================================

@pytest.mark.asyncio
@pytest.mark.xfail(reason=HOSTED_ONLY)
async def test_delivery_config_validation_max_timeout(client, auth_headers):
    """delivery.timeout_seconds=5000 returns 422 (max is 3600)."""
    body = _base_cue_payload(delivery={"timeout_seconds": 5000})
    resp = await client.post("/v1/cues", json=body, headers=auth_headers)
    assert resp.status_code == 422


@pytest.mark.asyncio
@pytest.mark.xfail(reason=HOSTED_ONLY)
async def test_delivery_config_accepted(client, auth_headers):
    """Valid delivery config accepted, round-trips in response."""
    body = _base_cue_payload(
        delivery={"timeout_seconds": 60, "outcome_deadline_seconds": 600},
    )
    resp = await client.post("/v1/cues", json=body, headers=auth_headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["delivery"] is not None
    assert data["delivery"]["timeout_seconds"] == 60
    assert data["delivery"]["outcome_deadline_seconds"] == 600


@pytest.mark.asyncio
@pytest.mark.xfail(reason=HOSTED_ONLY)
async def test_delivery_config_update(client, auth_headers):
    """PATCH /v1/cues/{id} with delivery config updates the cue."""
    create_resp = await client.post("/v1/cues", json=_base_cue_payload(), headers=auth_headers)
    assert create_resp.status_code == 201
    cue_id = create_resp.json()["id"]

    patch_resp = await client.patch(
        f"/v1/cues/{cue_id}",
        json={"delivery": {"timeout_seconds": 45, "outcome_deadline_seconds": 900}},
        headers=auth_headers,
    )
    assert patch_resp.status_code == 200
    data = patch_resp.json()
    assert data["delivery"]["timeout_seconds"] == 45
    assert data["delivery"]["outcome_deadline_seconds"] == 900


@pytest.mark.asyncio
@pytest.mark.xfail(reason=HOSTED_ONLY)
async def test_alerts_config_accepted(client, auth_headers):
    """alerts config accepted and round-trips in response."""
    body = _base_cue_payload(
        alerts={"consecutive_failures": 5, "missed_window_multiplier": 2},
    )
    resp = await client.post("/v1/cues", json=body, headers=auth_headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["alerts"] is not None
    assert data["alerts"]["consecutive_failures"] == 5
    assert data["alerts"]["missed_window_multiplier"] == 2


@pytest.mark.asyncio
async def test_delivery_config_defaults(client, auth_headers):
    """Cue created without delivery field has null delivery in response."""
    body = _base_cue_payload()
    resp = await client.post("/v1/cues", json=body, headers=auth_headers)
    assert resp.status_code == 201
    assert resp.json().get("delivery") is None


@pytest.mark.asyncio
async def test_alerts_config_defaults(client, auth_headers):
    """Cue created without alerts field has null alerts in response."""
    body = _base_cue_payload()
    resp = await client.post("/v1/cues", json=body, headers=auth_headers)
    assert resp.status_code == 201
    assert resp.json().get("alerts") is None


@pytest.mark.asyncio
@pytest.mark.xfail(reason=HOSTED_ONLY)
async def test_delivery_config_in_get(client, auth_headers):
    """delivery config round-trips through GET /v1/cues/{id}."""
    body = _base_cue_payload(
        delivery={"timeout_seconds": 90, "outcome_deadline_seconds": 1200},
    )
    create_resp = await client.post("/v1/cues", json=body, headers=auth_headers)
    assert create_resp.status_code == 201
    cue_id = create_resp.json()["id"]

    get_resp = await client.get(f"/v1/cues/{cue_id}", headers=auth_headers)
    assert get_resp.status_code == 200
    d = get_resp.json().get("delivery") or {}
    assert d["timeout_seconds"] == 90
    assert d["outcome_deadline_seconds"] == 1200


# ===========================================================================
# FIX 2 — _parse_outcome_from_response unit tests
# Hosted-only feature: not in cueapi-core/worker/tasks.py
# These are xfail — they document expected behavior when the feature lands.
# ===========================================================================

@pytest.mark.xfail(reason="_parse_outcome_from_response is hosted-only, not in cueapi-core")
def test_parse_outcome_pending_when_no_body():
    from worker.tasks import _parse_outcome_from_response
    assert _parse_outcome_from_response(None) is None


@pytest.mark.xfail(reason="_parse_outcome_from_response is hosted-only")
def test_outcome_read_from_response_body_success():
    from worker.tasks import _parse_outcome_from_response
    result = _parse_outcome_from_response('{"success": true, "result": "done"}')
    assert result["outcome_success"] is True
    assert result["outcome_result"] == "done"


@pytest.mark.xfail(reason="_parse_outcome_from_response is hosted-only")
def test_outcome_read_from_response_body_failure():
    from worker.tasks import _parse_outcome_from_response
    result = _parse_outcome_from_response('{"success": false, "error": "boom"}')
    assert result["outcome_success"] is False
    assert result["outcome_error"] == "boom"


@pytest.mark.xfail(reason="_parse_outcome_from_response is hosted-only")
def test_outcome_falls_back_when_no_success_field():
    from worker.tasks import _parse_outcome_from_response
    assert _parse_outcome_from_response('{"data": 42}') is None


@pytest.mark.xfail(reason="_parse_outcome_from_response is hosted-only")
def test_outcome_falls_back_when_not_json():
    from worker.tasks import _parse_outcome_from_response
    assert _parse_outcome_from_response("<html>error</html>") is None


@pytest.mark.xfail(reason="_parse_outcome_from_response is hosted-only")
def test_outcome_falls_back_when_empty():
    from worker.tasks import _parse_outcome_from_response
    assert _parse_outcome_from_response("") is None


@pytest.mark.xfail(reason="_parse_outcome_from_response is hosted-only")
def test_result_field_stored_as_outcome_result():
    from worker.tasks import _parse_outcome_from_response
    result = _parse_outcome_from_response('{"success": true, "result": "tweet posted"}')
    assert result["outcome_result"] == "tweet posted"


# ===========================================================================
# FIX 3 — Alert config ranges, combined configs, DB columns (6 tests)
# ===========================================================================

@pytest.mark.asyncio
@pytest.mark.xfail(reason=HOSTED_ONLY)
async def test_alert_config_consecutive_failures_zero_rejected(client, auth_headers):
    """consecutive_failures=0 is rejected with 422 (min is 1)."""
    body = _base_cue_payload(
        alerts={"consecutive_failures": 0, "missed_window_multiplier": 2},
    )
    resp = await client.post("/v1/cues", json=body, headers=auth_headers)
    assert resp.status_code == 422


@pytest.mark.asyncio
@pytest.mark.xfail(reason=HOSTED_ONLY)
async def test_alert_config_missed_window_zero_rejected(client, auth_headers):
    """missed_window_multiplier=0 is rejected with 422 (min is 1)."""
    body = _base_cue_payload(
        alerts={"consecutive_failures": 3, "missed_window_multiplier": 0},
    )
    resp = await client.post("/v1/cues", json=body, headers=auth_headers)
    assert resp.status_code == 422


@pytest.mark.asyncio
@pytest.mark.xfail(reason=HOSTED_ONLY)
async def test_alert_config_valid(client, auth_headers):
    """consecutive_failures=5, missed_window_multiplier=3 accepted."""
    body = _base_cue_payload(
        alerts={"consecutive_failures": 5, "missed_window_multiplier": 3},
    )
    resp = await client.post("/v1/cues", json=body, headers=auth_headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["alerts"]["consecutive_failures"] == 5
    assert data["alerts"]["missed_window_multiplier"] == 3


@pytest.mark.asyncio
@pytest.mark.xfail(reason=HOSTED_ONLY)
async def test_delivery_and_alerts_combined(client, auth_headers):
    """Cue with both delivery and alerts — both present in GET response."""
    body = _base_cue_payload(
        delivery={"timeout_seconds": 30, "outcome_deadline_seconds": 300},
        alerts={"consecutive_failures": 3, "missed_window_multiplier": 2},
    )
    create_resp = await client.post("/v1/cues", json=body, headers=auth_headers)
    assert create_resp.status_code == 201
    cue_id = create_resp.json()["id"]

    get_resp = await client.get(f"/v1/cues/{cue_id}", headers=auth_headers)
    assert get_resp.status_code == 200
    data = get_resp.json()

    assert data["delivery"]["timeout_seconds"] == 30
    assert data["delivery"]["outcome_deadline_seconds"] == 300
    assert data["alerts"]["consecutive_failures"] == 3
    assert data["alerts"]["missed_window_multiplier"] == 2


@pytest.mark.asyncio
@pytest.mark.xfail(reason=HOSTED_ONLY)
async def test_delivery_config_partial_patch(client, auth_headers):
    """PATCH adds delivery to a cue that didn't have it."""
    create_resp = await client.post("/v1/cues", json=_base_cue_payload(), headers=auth_headers)
    assert create_resp.status_code == 201
    cue_id = create_resp.json()["id"]
    assert create_resp.json().get("delivery") is None

    patch_resp = await client.patch(
        f"/v1/cues/{cue_id}",
        json={"delivery": {"timeout_seconds": 120, "outcome_deadline_seconds": 600}},
        headers=auth_headers,
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["delivery"]["timeout_seconds"] == 120


@pytest.mark.asyncio
@pytest.mark.xfail(reason=HOSTED_ONLY)
async def test_outcome_deadline_columns_exist(registered_user, db_session):
    """outcome_deadline_seconds and outcome_deadline_at columns exist and persist correctly."""
    from app.models.user import User
    from app.utils.ids import hash_api_key
    from sqlalchemy import select

    api_key_hash = hash_api_key(registered_user["api_key"])
    result = await db_session.execute(select(User).where(User.api_key_hash == api_key_hash))
    user = result.scalar_one()

    cue_id = generate_cue_id()
    cue = Cue(
        id=cue_id,
        user_id=str(user.id),
        name=f"deadline-test-{uuid.uuid4().hex[:6]}",
        status="active",
        schedule_type="once",
        schedule_timezone="UTC",
        callback_url="https://example.com/hook",
        callback_method="POST",
        callback_headers={},
        payload={},
        retry_max_attempts=3,
        retry_backoff_minutes=5,
        next_run=datetime.now(timezone.utc),
    )
    db_session.add(cue)
    await db_session.flush()

    exec_id = uuid.uuid4()
    deadline_at = datetime(2099, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    execution = Execution(
        id=exec_id,
        cue_id=cue_id,
        scheduled_for=datetime.now(timezone.utc),
        status="pending",
        attempts=0,
        outcome_deadline_seconds=300,
        outcome_deadline_at=deadline_at,
    )
    db_session.add(execution)
    await db_session.commit()

    refreshed = await db_session.execute(select(Execution).where(Execution.id == exec_id))
    ex = refreshed.scalar_one()
    assert ex.outcome_deadline_seconds == 300
    assert ex.outcome_deadline_at == deadline_at
