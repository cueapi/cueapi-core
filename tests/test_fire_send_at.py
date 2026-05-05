"""Tests for §13 (Phase 12.1.7): per-fire scheduling on POST /v1/cues/{id}/fire.

Roadmap doc §13: optional `send_at` timestamp on fire that delays dispatch
until the time elapsed. Same shape as cue's per-cue schedule, but per-fire.

Ported from cueapi/cueapi#618. The private repo's test_fire_send_at.py
also covers a `payload_override` compose case; that field belongs to
private PR #575 (require_payload_override) which is a separate parity
port and not in this OSS port.

These tests pin:

1. No `send_at` (or omitted) → dispatch immediately (existing behavior).
2. `send_at` in the future → execution's ``scheduled_for`` is set to
   send_at; outbox row has ``scheduled_at`` set so the dispatcher's
   existing ``scheduled_at IS NULL OR scheduled_at <= now()`` filter
   gates dispatch until then.
3. `send_at` in the past → forgiving fallback to "fire now" (no error).
   ``scheduled_for`` set to now; outbox ``scheduled_at`` left NULL.
4. Invalid timestamps return 422.
5. Worker-transport cues don't create an outbox row but
   ``scheduled_for`` on the Execution row still reflects send_at.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.dispatch_outbox import DispatchOutbox
from app.models.execution import Execution


async def _create_cue(client, auth_headers, name="send-at-test"):
    resp = await client.post(
        "/v1/cues",
        json={
            "name": name,
            "schedule": {"type": "recurring", "cron": "0 * * * *"},
            "callback": {"url": "https://example.com/webhook"},
            "payload": {"task": "send_at_default"},
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_fire_no_send_at_dispatches_immediately(client, auth_headers, db_session):
    """Existing behavior preserved: no body or no send_at → outbox.scheduled_at NULL."""
    cue_id = await _create_cue(client, auth_headers, "send-at-immediate")

    resp = await client.post(f"/v1/cues/{cue_id}/fire", headers=auth_headers)
    assert resp.status_code == 200
    exec_id = resp.json()["id"]

    outbox = (
        await db_session.execute(
            select(DispatchOutbox).where(DispatchOutbox.execution_id == uuid.UUID(exec_id))
        )
    ).scalar_one()
    assert outbox.scheduled_at is None, (
        "no send_at → outbox.scheduled_at must be NULL so dispatcher fires immediately"
    )


@pytest.mark.asyncio
async def test_fire_send_at_future_delays_dispatch(client, auth_headers, db_session):
    cue_id = await _create_cue(client, auth_headers, "send-at-future")
    future = datetime.now(timezone.utc) + timedelta(hours=2)

    resp = await client.post(
        f"/v1/cues/{cue_id}/fire",
        json={"send_at": future.isoformat()},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    exec_id = body["id"]

    parsed = datetime.fromisoformat(body["scheduled_for"])
    assert abs((parsed - future).total_seconds()) < 1.0

    execution = (
        await db_session.execute(select(Execution).where(Execution.id == uuid.UUID(exec_id)))
    ).scalar_one()
    assert abs((execution.scheduled_for - future).total_seconds()) < 1.0

    outbox = (
        await db_session.execute(
            select(DispatchOutbox).where(DispatchOutbox.execution_id == uuid.UUID(exec_id))
        )
    ).scalar_one()
    assert outbox.scheduled_at is not None, "send_at in future → outbox.scheduled_at must be set"
    assert abs((outbox.scheduled_at - future).total_seconds()) < 1.0


@pytest.mark.asyncio
async def test_fire_send_at_past_falls_back_to_now(client, auth_headers, db_session):
    """Past timestamps are forgiving — no error, treated as 'fire now'.

    Idempotent: callers don't need to worry about clock skew or being
    a few ms late after computing a send_at locally.
    """
    cue_id = await _create_cue(client, auth_headers, "send-at-past")
    past = datetime.now(timezone.utc) - timedelta(hours=1)

    resp = await client.post(
        f"/v1/cues/{cue_id}/fire",
        json={"send_at": past.isoformat()},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    exec_id = resp.json()["id"]

    outbox = (
        await db_session.execute(
            select(DispatchOutbox).where(DispatchOutbox.execution_id == uuid.UUID(exec_id))
        )
    ).scalar_one()
    assert outbox.scheduled_at is None, (
        "send_at in past → forgiving fallback; outbox.scheduled_at must be NULL"
    )


@pytest.mark.asyncio
async def test_fire_send_at_invalid_timestamp_returns_422(client, auth_headers):
    """Pydantic catches malformed datetime strings."""
    cue_id = await _create_cue(client, auth_headers, "send-at-invalid")

    resp = await client.post(
        f"/v1/cues/{cue_id}/fire",
        json={"send_at": "not-a-date"},
        headers=auth_headers,
    )
    assert resp.status_code in (400, 422)


@pytest.mark.asyncio
async def test_fire_send_at_worker_transport_no_outbox(client, auth_headers, db_session):
    """Worker-transport cues don't create an outbox row, but ``scheduled_for``
    on the Execution row still reflects send_at so worker pull endpoints
    can filter by it (not done here — just pin the Execution shape)."""
    create = await client.post(
        "/v1/cues",
        json={
            "name": "send-at-worker",
            "schedule": {"type": "recurring", "cron": "0 * * * *"},
            "transport": "worker",
            "payload": {"task": "scheduled_worker_task"},
        },
        headers=auth_headers,
    )
    assert create.status_code == 201, create.text
    cue_id = create.json()["id"]

    future = datetime.now(timezone.utc) + timedelta(minutes=30)
    resp = await client.post(
        f"/v1/cues/{cue_id}/fire",
        json={"send_at": future.isoformat()},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    exec_id = resp.json()["id"]

    execution = (
        await db_session.execute(select(Execution).where(Execution.id == uuid.UUID(exec_id)))
    ).scalar_one()
    assert abs((execution.scheduled_for - future).total_seconds()) < 1.0

    outbox = (
        await db_session.execute(
            select(DispatchOutbox).where(DispatchOutbox.execution_id == uuid.UUID(exec_id))
        )
    ).scalar_one_or_none()
    assert outbox is None
