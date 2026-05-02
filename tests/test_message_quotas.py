"""Quota + rate-limit tests for the messaging primitive (Phase 2.11.6a).

Spec: `https://trydock.ai/mike/cueapi-messaging-primitive-v1-sp` §7 (Quotas + abuse) +
§13 D1 (separate quotas) + D5 (free=300/pro=5000/scale=50000).
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import update

from app.models import User


async def _make_agent(client, headers, slug=None):
    payload = {"display_name": f"Agent {uuid.uuid4().hex[:6]}", "metadata": {}}
    if slug:
        payload["slug"] = slug
    r = await client.post("/v1/agents", json=payload, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _from_header(agent):
    return {"X-Cueapi-From-Agent": agent["id"]}


async def _set_user_limits(
    db_session,
    registered_user,
    *,
    monthly_message_limit=None,
    plan=None,
):
    """Override the test user's limits.

    Setting ``plan='scale'`` bumps the per-minute message rate limit to
    300/min so quota / priority-high tests aren't bottlenecked by the
    free-tier 10/min limit before the test's specific limit binds.
    """
    email = registered_user["email"]
    values = {}
    if monthly_message_limit is not None:
        values["monthly_message_limit"] = monthly_message_limit
    if plan is not None:
        values["plan"] = plan
    if values:
        await db_session.execute(
            update(User).where(User.email == email).values(**values)
        )
        await db_session.commit()


@pytest.mark.asyncio
async def test_monthly_quota_blocks_at_limit(
    client, auth_headers, registered_user, db_session, redis_client
):
    """Setting limit=2 → 3rd send → 402 quota_exceeded."""
    await _set_user_limits(db_session, registered_user, monthly_message_limit=2, plan="scale")

    sender = await _make_agent(client, auth_headers, slug="q-s")
    recipient = await _make_agent(client, auth_headers, slug="q-r")

    # First two sends succeed.
    for i in range(2):
        r = await client.post(
            "/v1/messages",
            json={"to": recipient["id"], "body": f"m{i}"},
            headers={**auth_headers, **_from_header(sender)},
        )
        assert r.status_code == 201, r.text

    # Third send → 402.
    r3 = await client.post(
        "/v1/messages",
        json={"to": recipient["id"], "body": "third"},
        headers={**auth_headers, **_from_header(sender)},
    )
    assert r3.status_code == 402, r3.text
    assert r3.json()["error"]["code"] == "quota_exceeded"
    assert r3.json()["error"]["limit"] == 2
    assert r3.json()["error"]["current"] == 2


@pytest.mark.asyncio
async def test_quota_separate_from_execution_quota(
    client, auth_headers, registered_user, db_session, redis_client
):
    """Sending messages does NOT decrement the cue execution quota.

    Verifies the spec §13 D1 decision (separate quotas) by sending a
    message and confirming execution count stays at 0.
    """
    sender = await _make_agent(client, auth_headers, slug="sep-s")
    recipient = await _make_agent(client, auth_headers, slug="sep-r")

    r = await client.post(
        "/v1/messages",
        json={"to": recipient["id"], "body": "hi"},
        headers={**auth_headers, **_from_header(sender)},
    )
    assert r.status_code == 201

    # Hit /v1/usage and confirm executions counter is 0 — sending a
    # message must NOT touch the cue execution quota (§13 D1 separate).
    usage = await client.get("/v1/usage", headers=auth_headers)
    assert usage.status_code == 200
    body = usage.json()
    # The usage response nests execution stats under "executions".
    assert body["executions"]["used"] == 0


@pytest.mark.asyncio
async def test_quota_dedup_does_not_consume(
    client, auth_headers, registered_user, db_session, redis_client
):
    """Idempotency-Key dedup hit returns existing message — does NOT
    re-consume quota."""
    await _set_user_limits(db_session, registered_user, monthly_message_limit=2, plan="scale")

    sender = await _make_agent(client, auth_headers, slug="dd-s")
    recipient = await _make_agent(client, auth_headers, slug="dd-r")

    # Send 1 with idempotency key.
    r1 = await client.post(
        "/v1/messages",
        json={"to": recipient["id"], "body": "once"},
        headers={**auth_headers, **_from_header(sender), "Idempotency-Key": "dd-key"},
    )
    assert r1.status_code == 201

    # Send 2 (different message) — also counts.
    r2 = await client.post(
        "/v1/messages",
        json={"to": recipient["id"], "body": "two"},
        headers={**auth_headers, **_from_header(sender)},
    )
    assert r2.status_code == 201

    # Send 3: replay r1's idempotency key → dedup hit → 200, NOT counted.
    r3 = await client.post(
        "/v1/messages",
        json={"to": recipient["id"], "body": "once"},
        headers={**auth_headers, **_from_header(sender), "Idempotency-Key": "dd-key"},
    )
    assert r3.status_code == 200  # dedup hit
    assert r3.json()["id"] == r1.json()["id"]

    # Send 4: new message, key=fresh. Quota was 2, two distinct messages
    # already sent → 402.
    r4 = await client.post(
        "/v1/messages",
        json={"to": recipient["id"], "body": "fresh"},
        headers={**auth_headers, **_from_header(sender)},
    )
    assert r4.status_code == 402


@pytest.mark.asyncio
async def test_priority_high_sender_rate_limit(
    client, auth_headers, registered_user, db_session, redis_client
):
    """priority>3 limited to 10/hour/sender. 11th high-priority send → 429."""
    # Plan=scale lifts the per-minute rate limit (300/min) so the
    # priority-high cap (10/hour) is the binding constraint.
    await _set_user_limits(db_session, registered_user, plan="scale")

    sender = await _make_agent(client, auth_headers, slug="ph-s")
    recipient = await _make_agent(client, auth_headers, slug="ph-r")

    # Issue 10 priority-4 messages — all should succeed (sender + pair
    # limits both 5/10 at hour granularity, so first 5 succeed at p=4
    # then next 5 are downgraded to p=3 silently per pair limit).
    success_count = 0
    for i in range(10):
        r = await client.post(
            "/v1/messages",
            json={"to": recipient["id"], "body": f"hp{i}", "priority": 4},
            headers={**auth_headers, **_from_header(sender)},
        )
        if r.status_code in (201, 200):
            success_count += 1
    assert success_count == 10

    # 11th → 429 sender-side hard cap.
    r11 = await client.post(
        "/v1/messages",
        json={"to": recipient["id"], "body": "hp11", "priority": 5},
        headers={**auth_headers, **_from_header(sender)},
    )
    assert r11.status_code == 429
    assert r11.json()["error"]["code"] == "priority_high_rate_limit"


@pytest.mark.asyncio
async def test_priority_high_pair_downgrade(
    client, auth_headers, registered_user, db_session, redis_client
):
    """Per-pair priority>3 cap is 5/hour. 6th pair-targeted high-priority
    is silently downgraded to priority=3 with X-CueAPI-Priority-Downgraded."""
    await _set_user_limits(db_session, registered_user, plan="scale")
    sender = await _make_agent(client, auth_headers, slug="pd-s")
    recipient = await _make_agent(client, auth_headers, slug="pd-r")

    # First 5 priority-4 to this pair → all priority=4 preserved.
    for i in range(5):
        r = await client.post(
            "/v1/messages",
            json={"to": recipient["id"], "body": f"hp{i}", "priority": 4},
            headers={**auth_headers, **_from_header(sender)},
        )
        assert r.status_code == 201, r.text
        assert r.json()["priority"] == 4
        assert "X-CueAPI-Priority-Downgraded" not in {k.lower() for k in r.headers}

    # 6th to same pair → downgraded to 3 + header signal.
    r6 = await client.post(
        "/v1/messages",
        json={"to": recipient["id"], "body": "hp6", "priority": 4},
        headers={**auth_headers, **_from_header(sender)},
    )
    assert r6.status_code == 201
    assert r6.json()["priority"] == 3  # silently downgraded
    # Header check (case-insensitive — httpx exposes lower-cased keys).
    headers_lower = {k.lower(): v for k, v in r6.headers.items()}
    assert headers_lower.get("x-cueapi-priority-downgraded") == "true"


@pytest.mark.asyncio
async def test_priority_3_no_downgrade(
    client, auth_headers, registered_user, db_session, redis_client
):
    """priority<=3 is never affected by the high-priority caps."""
    await _set_user_limits(db_session, registered_user, plan="scale")
    sender = await _make_agent(client, auth_headers, slug="lp-s")
    recipient = await _make_agent(client, auth_headers, slug="lp-r")

    for i in range(20):
        r = await client.post(
            "/v1/messages",
            json={"to": recipient["id"], "body": f"lp{i}", "priority": 3},
            headers={**auth_headers, **_from_header(sender)},
        )
        assert r.status_code == 201
        assert r.json()["priority"] == 3
