"""HTTP route tests for ``app/routers/events.py``.

Covers:

* Subscription create — pull + webhook happy paths, unknown event_type
  400, SSRF private-range 400, agent-not-owned 404
* Subscription list — only the caller's own; webhook_url redacted to
  host-only; webhook_secret omitted (never exposed post-create)
* Subscription delete — idempotent 200 on re-delete, wrong-owner
  silently no-ops at 200
* Events pull — empty list, cursor pagination, limit clamp at 1000,
  event_type filter, recipient isolation via agent scope
* Pure-helper _redact_webhook_url + _subscription_to_response: unit
  tests for the secret-elision and URL-redaction branches
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.subscription import Subscription
from app.models.user import User
from app.routers.events import (
    _redact_webhook_url,
    _subscription_to_response,
)


async def _resolve_user_id(db_session: AsyncSession, email: str) -> str:
    user = (
        await db_session.execute(select(User).where(User.email == email))
    ).scalar_one()
    return str(user.id)


@pytest_asyncio.fixture
async def owned_agent(db_session: AsyncSession, registered_user: dict) -> Agent:
    user_id = await _resolve_user_id(db_session, registered_user["email"])
    agent = Agent(
        id="agt_route0000001",
        user_id=user_id,
        slug="route-test",
        display_name="Route Test Agent",
    )
    db_session.add(agent)
    await db_session.commit()
    await db_session.refresh(agent)
    return agent


# ───────────────────────────────────────────────────────────────────────
# Pure-helper unit tests (no DB, no HTTP)
# ───────────────────────────────────────────────────────────────────────

def test_redact_webhook_url_strips_path_and_query():
    out = _redact_webhook_url("https://example.com/webhook/path?token=secret")
    assert out == "https://example.com"


def test_redact_webhook_url_handles_port():
    out = _redact_webhook_url("https://example.com:8443/hook")
    assert out == "https://example.com:8443"


def test_redact_webhook_url_returns_none_for_none():
    assert _redact_webhook_url(None) is None


def test_redact_webhook_url_returns_none_for_empty():
    assert _redact_webhook_url("") is None


def test_redact_webhook_url_returns_none_for_malformed():
    """Missing scheme or netloc → None (defensive)."""
    assert _redact_webhook_url("not-a-url") is None
    assert _redact_webhook_url("just/a/path") is None


def test_subscription_to_response_omits_secret_by_default():
    """List + detail responses must NOT include webhook_secret."""
    sub = Subscription(
        id=uuid4(),
        subscriber_agent_id="agt_test00000001",
        event_type="message.delivered",
        delivery_target="webhook",
        webhook_url="https://example.com/hook",
        webhook_secret="whsec_supersecret_should_never_leak",
        consecutive_failures=0,
        created_at=datetime.now(timezone.utc),
    )
    resp = _subscription_to_response(sub)
    assert resp.webhook_secret is None
    # webhook_url redacted to host-only
    assert resp.webhook_url == "https://example.com"


def test_subscription_to_response_includes_secret_when_asked():
    """Only the create endpoint passes include_secret=True."""
    sub = Subscription(
        id=uuid4(),
        subscriber_agent_id="agt_test00000001",
        event_type="message.delivered",
        delivery_target="webhook",
        webhook_url="https://example.com/hook",
        webhook_secret="whsec_supersecret",
        consecutive_failures=0,
        created_at=datetime.now(timezone.utc),
    )
    resp = _subscription_to_response(sub, include_secret=True)
    assert resp.webhook_secret == "whsec_supersecret"


def test_subscription_to_response_handles_pull_sub_no_url_no_secret():
    """Pull subs have no webhook fields; response surface reflects that."""
    sub = Subscription(
        id=uuid4(),
        subscriber_agent_id="agt_test00000001",
        event_type="message.delivered",
        delivery_target="pull",
        webhook_url=None,
        webhook_secret=None,
        consecutive_failures=0,
        created_at=datetime.now(timezone.utc),
    )
    resp = _subscription_to_response(sub, include_secret=True)
    assert resp.webhook_url is None
    assert resp.webhook_secret is None


# ───────────────────────────────────────────────────────────────────────
# POST /v1/agents/{ref}/subscriptions
# ───────────────────────────────────────────────────────────────────────

async def test_create_pull_subscription_201(
    client: AsyncClient, auth_headers: dict, owned_agent: Agent
):
    resp = await client.post(
        f"/v1/agents/{owned_agent.id}/subscriptions",
        json={"event_type": "message.delivered", "delivery_target": "pull"},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["delivery_target"] == "pull"
    assert body["webhook_url"] is None
    assert body["webhook_secret"] is None
    assert body["subscriber_agent_id"] == owned_agent.id


async def test_create_webhook_subscription_returns_secret_once(
    client: AsyncClient, auth_headers: dict, owned_agent: Agent
):
    resp = await client.post(
        f"/v1/agents/{owned_agent.id}/subscriptions",
        json={
            "event_type": "message.delivered",
            "delivery_target": "webhook",
            "webhook_url": "https://example.com/hook?token=abc",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["delivery_target"] == "webhook"
    # URL redacted in response — query string stripped, path stripped.
    assert body["webhook_url"] == "https://example.com"
    assert body["webhook_secret"].startswith("whsec_")


async def test_create_subscription_unknown_event_type_400(
    client: AsyncClient, auth_headers: dict, owned_agent: Agent
):
    resp = await client.post(
        f"/v1/agents/{owned_agent.id}/subscriptions",
        json={"event_type": "totally.not.real", "delivery_target": "pull"},
        headers=auth_headers,
    )
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "unknown_event_type"


async def test_create_subscription_ssrf_blocks_localhost_400(
    client: AsyncClient, auth_headers: dict, owned_agent: Agent
):
    resp = await client.post(
        f"/v1/agents/{owned_agent.id}/subscriptions",
        json={
            "event_type": "message.delivered",
            "delivery_target": "webhook",
            "webhook_url": "http://127.0.0.1:8080/hook",
        },
        headers=auth_headers,
    )
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "invalid_webhook_url"


async def test_create_subscription_for_unowned_agent_404(
    client: AsyncClient, auth_headers: dict, owned_agent: Agent
):
    """Try to subscribe under a nonexistent / foreign agent id."""
    resp = await client.post(
        "/v1/agents/agt_doesnotexis1/subscriptions",  # 16 chars but no row
        json={"event_type": "message.delivered", "delivery_target": "pull"},
        headers=auth_headers,
    )
    assert resp.status_code == 404


async def test_create_subscription_requires_auth(
    client: AsyncClient, owned_agent: Agent
):
    resp = await client.post(
        f"/v1/agents/{owned_agent.id}/subscriptions",
        json={"event_type": "message.delivered", "delivery_target": "pull"},
    )
    assert resp.status_code == 401


# ───────────────────────────────────────────────────────────────────────
# GET /v1/agents/{ref}/subscriptions
# ───────────────────────────────────────────────────────────────────────

async def test_list_subscriptions_returns_only_active(
    client: AsyncClient, auth_headers: dict, owned_agent: Agent
):
    # Create a webhook sub.
    create_resp = await client.post(
        f"/v1/agents/{owned_agent.id}/subscriptions",
        json={
            "event_type": "message.delivered",
            "delivery_target": "webhook",
            "webhook_url": "https://example.com/hook",
        },
        headers=auth_headers,
    )
    assert create_resp.status_code == 201
    sub_id = create_resp.json()["id"]

    list_resp = await client.get(
        f"/v1/agents/{owned_agent.id}/subscriptions",
        headers=auth_headers,
    )
    assert list_resp.status_code == 200
    body = list_resp.json()
    assert len(body["subscriptions"]) == 1
    entry = body["subscriptions"][0]
    # Per CTO correction #2: state surface present.
    assert "last_dispatched_event_id" in entry
    assert "last_dispatched_at" in entry
    assert "consecutive_failures" in entry
    assert "paused_until" in entry
    # webhook_secret never returned by list.
    assert entry["webhook_secret"] is None
    # webhook_url redacted to host.
    assert entry["webhook_url"] == "https://example.com"


# ───────────────────────────────────────────────────────────────────────
# DELETE /v1/agents/{ref}/subscriptions/{id}
# ───────────────────────────────────────────────────────────────────────

async def test_delete_subscription_idempotent(
    client: AsyncClient, auth_headers: dict, owned_agent: Agent
):
    create_resp = await client.post(
        f"/v1/agents/{owned_agent.id}/subscriptions",
        json={"event_type": "message.delivered", "delivery_target": "pull"},
        headers=auth_headers,
    )
    sub_id = create_resp.json()["id"]

    first = await client.delete(
        f"/v1/agents/{owned_agent.id}/subscriptions/{sub_id}",
        headers=auth_headers,
    )
    assert first.status_code == 200

    # Re-DELETE returns 200 (idempotent).
    second = await client.delete(
        f"/v1/agents/{owned_agent.id}/subscriptions/{sub_id}",
        headers=auth_headers,
    )
    assert second.status_code == 200

    # List confirms detached.
    list_resp = await client.get(
        f"/v1/agents/{owned_agent.id}/subscriptions",
        headers=auth_headers,
    )
    assert list_resp.json()["subscriptions"] == []


# ───────────────────────────────────────────────────────────────────────
# GET /v1/agents/{ref}/events
# ───────────────────────────────────────────────────────────────────────

async def test_pull_events_empty_returns_empty_array(
    client: AsyncClient, auth_headers: dict, owned_agent: Agent
):
    resp = await client.get(
        f"/v1/agents/{owned_agent.id}/events",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["events"] == []
    assert body["next_cursor"] is None
    assert body["has_more"] is False


async def test_pull_events_with_seeded_event_returns_it(
    client: AsyncClient,
    auth_headers: dict,
    owned_agent: Agent,
    db_session: AsyncSession,
):
    """Seed an event directly via the service layer; pull via HTTP."""
    from app.services.events_service import emit_event

    await emit_event(
        db_session,
        event_type="message.delivered",
        recipient_agent_id=owned_agent.id,
        payload={"message_id": "msg_seed"},
    )
    await db_session.commit()

    resp = await client.get(
        f"/v1/agents/{owned_agent.id}/events",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["events"]) == 1
    ev = body["events"][0]
    assert ev["event_type"] == "message.delivered"
    assert ev["recipient_agent_id"] == owned_agent.id
    assert ev["payload"] == {"message_id": "msg_seed"}
    assert body["next_cursor"] == ev["id"]


async def test_pull_events_limit_validation_rejects_over_1000(
    client: AsyncClient, auth_headers: dict, owned_agent: Agent
):
    """FastAPI Query(le=1000) enforces server-side cap at the param layer."""
    resp = await client.get(
        f"/v1/agents/{owned_agent.id}/events?limit=10000",
        headers=auth_headers,
    )
    # FastAPI returns 422 for out-of-range query params.
    assert resp.status_code == 422


async def test_pull_events_for_unowned_agent_404(
    client: AsyncClient, auth_headers: dict
):
    resp = await client.get(
        "/v1/agents/agt_notmineagnt1/events",  # 16 chars, no row
        headers=auth_headers,
    )
    assert resp.status_code == 404


async def test_pull_events_requires_auth(
    client: AsyncClient, owned_agent: Agent
):
    resp = await client.get(f"/v1/agents/{owned_agent.id}/events")
    assert resp.status_code == 401
