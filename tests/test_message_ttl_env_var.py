"""MESSAGE_TTL_DAYS settings env-var refactor.

Per CWS-2026-05-08 Item 4 lock: the previously-hardcoded 30-day TTL
is now `settings.MESSAGE_TTL_DAYS`, tunable per deployment without
patching code. New messages use the env-var value at creation time;
existing messages keep their baked-in `expires_at` (no migration).

Verifies:

* Default: created messages have ``expires_at`` ≈ now + 30 days.
* Override: setting `MESSAGE_TTL_DAYS=7` results in ``expires_at`` ≈
  now + 7 days for newly created messages.
* No regression on existing messages — pre-existing rows keep their
  baked-in `expires_at` regardless of the env-var value.
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings


@contextmanager
def _patch_setting(name: str, value):
    original = getattr(settings, name)
    setattr(settings, name, value)
    try:
        yield
    finally:
        setattr(settings, name, original)


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
async def test_default_ttl_is_30_days(client, auth_headers):
    sender = await _make_agent(client, auth_headers, slug=f"s-{uuid.uuid4().hex[:6]}")
    recipient = await _make_agent(
        client, auth_headers, slug=f"r-{uuid.uuid4().hex[:6]}"
    )
    before = datetime.now(timezone.utc)
    r = await client.post(
        "/v1/messages",
        json={"to": recipient["id"], "body": "default-ttl"},
        headers={**auth_headers, **_from_header(sender)},
    )
    after = datetime.now(timezone.utc)
    assert r.status_code == 201, r.text
    expires_str = r.json()["expires_at"]
    expires_at = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))

    # ~30 days ahead, with a 1s window on either side for clock movement.
    expected_min = before + timedelta(days=30) - timedelta(seconds=1)
    expected_max = after + timedelta(days=30) + timedelta(seconds=1)
    assert expected_min <= expires_at <= expected_max, (
        f"expires_at={expires_at} outside [{expected_min}, {expected_max}]"
    )


@pytest.mark.asyncio
async def test_override_ttl_via_settings(client, auth_headers):
    sender = await _make_agent(client, auth_headers, slug=f"s-{uuid.uuid4().hex[:6]}")
    recipient = await _make_agent(
        client, auth_headers, slug=f"r-{uuid.uuid4().hex[:6]}"
    )

    with _patch_setting("MESSAGE_TTL_DAYS", 7):
        before = datetime.now(timezone.utc)
        r = await client.post(
            "/v1/messages",
            json={"to": recipient["id"], "body": "short-ttl"},
            headers={**auth_headers, **_from_header(sender)},
        )
        after = datetime.now(timezone.utc)
        assert r.status_code == 201, r.text
        expires_str = r.json()["expires_at"]
        expires_at = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))

        # ~7 days ahead.
        expected_min = before + timedelta(days=7) - timedelta(seconds=1)
        expected_max = after + timedelta(days=7) + timedelta(seconds=1)
        assert expected_min <= expires_at <= expected_max, (
            f"expires_at={expires_at} outside [{expected_min}, {expected_max}] "
            f"with MESSAGE_TTL_DAYS=7"
        )


@pytest.mark.asyncio
async def test_long_ttl_via_settings(client, auth_headers):
    """Self-host integrators tuning for human-in-the-loop UX (90d retention)."""
    sender = await _make_agent(client, auth_headers, slug=f"s-{uuid.uuid4().hex[:6]}")
    recipient = await _make_agent(
        client, auth_headers, slug=f"r-{uuid.uuid4().hex[:6]}"
    )

    with _patch_setting("MESSAGE_TTL_DAYS", 90):
        before = datetime.now(timezone.utc)
        r = await client.post(
            "/v1/messages",
            json={"to": recipient["id"], "body": "long-ttl"},
            headers={**auth_headers, **_from_header(sender)},
        )
        after = datetime.now(timezone.utc)
        assert r.status_code == 201, r.text
        expires_str = r.json()["expires_at"]
        expires_at = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))

        expected_min = before + timedelta(days=90) - timedelta(seconds=1)
        expected_max = after + timedelta(days=90) + timedelta(seconds=1)
        assert expected_min <= expires_at <= expected_max


@pytest.mark.asyncio
async def test_existing_messages_keep_baked_in_expires_at(client, auth_headers):
    """Pre-existing messages aren't retroactively repriced when the env-var changes.

    Set MESSAGE_TTL_DAYS=30 (default), create message → expires_at ≈ +30d.
    Bump MESSAGE_TTL_DAYS=7. Re-fetch the original message; expires_at
    must STILL be the original ≈ +30d value (DB-stored, not recomputed).
    """
    sender = await _make_agent(client, auth_headers, slug=f"s-{uuid.uuid4().hex[:6]}")
    recipient = await _make_agent(
        client, auth_headers, slug=f"r-{uuid.uuid4().hex[:6]}"
    )

    # Create at default TTL.
    r = await client.post(
        "/v1/messages",
        json={"to": recipient["id"], "body": "originally-30d"},
        headers={**auth_headers, **_from_header(sender)},
    )
    assert r.status_code == 201
    msg_id = r.json()["id"]
    original_expires_str = r.json()["expires_at"]

    # Now flip the setting and re-fetch — value must not change.
    with _patch_setting("MESSAGE_TTL_DAYS", 7):
        get_r = await client.get(f"/v1/messages/{msg_id}", headers=auth_headers)
        assert get_r.status_code == 200
        fetched_expires_str = get_r.json()["expires_at"]
        assert fetched_expires_str == original_expires_str, (
            f"existing message's expires_at changed after env flip: "
            f"original={original_expires_str}, fetched={fetched_expires_str}"
        )
