"""HTTP-level tests for the Identity router (Phase 2.11.2).

Spec: `https://trydock.ai/mike/cueapi-messaging-primitive-v1-sp` §2 (Identity primitive) +
§10.1 (target test list) + §13 D11 (slug-form delimiter at-sign).

Covers:

* CRUD: POST/GET/PATCH/DELETE /v1/agents
* Slug uniqueness, auto-derivation, collision-suffix, reserved slugs
* webhook_url / webhook_secret pairing + SSRF rejection
* Slug-form addressing (``agent_slug@user_slug``)
* Soft-delete + ``include_deleted`` query
* Per-user isolation (other user's agent → 404)
* Webhook secret retrieval + rotation + confirmation header

Schema-level constraints (UNIQUE, CheckConstraint, etc.) are exercised
in tests/test_messaging_schema.py at the ORM level. These tests
exercise the HTTP surface end-to-end.
"""
from __future__ import annotations

import uuid

import pytest


def _agent_payload(**overrides):
    base = {"display_name": "Test Agent", "metadata": {}}
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_create_agent_minimal(client, auth_headers):
    r = await client.post(
        "/v1/agents",
        json=_agent_payload(display_name="My Agent"),
        headers=auth_headers,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"].startswith("agt_")
    assert len(body["id"]) == 16
    assert body["display_name"] == "My Agent"
    assert body["slug"] == "my-agent"  # auto-derived
    assert body["status"] == "online"
    assert body["webhook_url"] is None
    assert body["webhook_secret"] is None  # null when no webhook_url
    assert body["deleted_at"] is None
    assert body["metadata"] == {}


@pytest.mark.asyncio
async def test_create_agent_with_explicit_slug(client, auth_headers):
    r = await client.post(
        "/v1/agents",
        json=_agent_payload(slug="cue-mac-app", display_name="Cue Mac App"),
        headers=auth_headers,
    )
    assert r.status_code == 201
    assert r.json()["slug"] == "cue-mac-app"


@pytest.mark.asyncio
async def test_create_agent_slug_collision_409(client, auth_headers):
    a = await client.post(
        "/v1/agents", json=_agent_payload(slug="dock-demo"), headers=auth_headers
    )
    assert a.status_code == 201
    b = await client.post(
        "/v1/agents", json=_agent_payload(slug="dock-demo"), headers=auth_headers
    )
    assert b.status_code == 409
    assert b.json()["error"]["code"] == "slug_taken"


@pytest.mark.asyncio
async def test_create_agent_slug_auto_collision_suffixed(client, auth_headers):
    """Two agents with the same display_name → second gets a suffix."""
    a = await client.post(
        "/v1/agents", json=_agent_payload(display_name="My Agent"), headers=auth_headers
    )
    b = await client.post(
        "/v1/agents", json=_agent_payload(display_name="My Agent"), headers=auth_headers
    )
    assert a.status_code == 201
    assert b.status_code == 201
    assert a.json()["slug"] == "my-agent"
    assert b.json()["slug"].startswith("my-agent-")
    assert a.json()["slug"] != b.json()["slug"]


@pytest.mark.asyncio
async def test_create_agent_with_webhook_returns_secret_inline(client, auth_headers):
    r = await client.post(
        "/v1/agents",
        json=_agent_payload(
            display_name="Push Receiver",
            webhook_url="https://example.com/webhook/in",
        ),
        headers=auth_headers,
    )
    assert r.status_code == 201
    body = r.json()
    assert body["webhook_url"] == "https://example.com/webhook/in"
    assert body["webhook_secret"].startswith("whsec_")


@pytest.mark.asyncio
async def test_create_agent_webhook_url_ssrf_blocked(client, auth_headers):
    """Private IP rejected via SSRF defense."""
    r = await client.post(
        "/v1/agents",
        json=_agent_payload(
            display_name="ssrf",
            webhook_url="http://127.0.0.1:8080/wh",
        ),
        headers=auth_headers,
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_callback_url"


@pytest.mark.asyncio
async def test_create_agent_reserved_slug_rejected(client, auth_headers):
    r = await client.post(
        "/v1/agents", json=_agent_payload(slug="admin"), headers=auth_headers
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "reserved_slug"


@pytest.mark.asyncio
async def test_get_agent_by_opaque_id(client, auth_headers):
    created = await client.post(
        "/v1/agents", json=_agent_payload(slug="readme"), headers=auth_headers
    )
    aid = created.json()["id"]
    r = await client.get(f"/v1/agents/{aid}", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["id"] == aid
    # Read path never reveals the secret.
    assert r.json()["webhook_secret"] is None


@pytest.mark.asyncio
async def test_get_agent_by_slug_form(client, auth_headers, registered_user):
    """Spec §13 D11: slug-form is `agent_slug@user_slug` (at-sign)."""
    await client.post(
        "/v1/agents", json=_agent_payload(slug="my-bot"), headers=auth_headers
    )
    # Discover the user-slug from /v1/auth/me.
    me = await client.get("/v1/auth/me", headers=auth_headers)
    user_slug = me.json().get("slug")
    assert user_slug, "user.slug must be exposed by /v1/auth/me"

    r = await client.get(f"/v1/agents/my-bot@{user_slug}", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["slug"] == "my-bot"


@pytest.mark.asyncio
async def test_get_agent_slug_form_invalid_400(client, auth_headers):
    r = await client.get("/v1/agents/foo@bar@baz", headers=auth_headers)
    # Pydantic Path doesn't reject; service layer 400s.
    assert r.status_code == 404 or r.status_code == 400


@pytest.mark.asyncio
async def test_get_agent_other_user_404(client, auth_headers, other_auth_headers):
    """Per-user isolation: other user's agent is invisible (404 not 403)."""
    a = await client.post(
        "/v1/agents", json=_agent_payload(slug="theirs"), headers=other_auth_headers
    )
    aid = a.json()["id"]
    # Caller has different api key — agent isn't theirs → 404.
    r = await client.get(f"/v1/agents/{aid}", headers=auth_headers)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_list_agents_excludes_deleted_by_default(client, auth_headers):
    a = await client.post(
        "/v1/agents", json=_agent_payload(slug="alive"), headers=auth_headers
    )
    b = await client.post(
        "/v1/agents", json=_agent_payload(slug="dead"), headers=auth_headers
    )
    await client.delete(f"/v1/agents/{b.json()['id']}", headers=auth_headers)

    r = await client.get("/v1/agents", headers=auth_headers)
    assert r.status_code == 200
    slugs = [agent["slug"] for agent in r.json()["agents"]]
    assert "alive" in slugs
    assert "dead" not in slugs


@pytest.mark.asyncio
async def test_list_agents_include_deleted(client, auth_headers):
    a = await client.post(
        "/v1/agents", json=_agent_payload(slug="alive2"), headers=auth_headers
    )
    b = await client.post(
        "/v1/agents", json=_agent_payload(slug="dead2"), headers=auth_headers
    )
    await client.delete(f"/v1/agents/{b.json()['id']}", headers=auth_headers)

    r = await client.get("/v1/agents?include_deleted=true", headers=auth_headers)
    slugs = [agent["slug"] for agent in r.json()["agents"]]
    assert "alive2" in slugs
    assert "dead2" in slugs


@pytest.mark.asyncio
async def test_patch_agent_display_name(client, auth_headers):
    a = await client.post(
        "/v1/agents", json=_agent_payload(slug="p1", display_name="Original"),
        headers=auth_headers,
    )
    aid = a.json()["id"]
    r = await client.patch(
        f"/v1/agents/{aid}",
        json={"display_name": "Renamed"},
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert r.json()["display_name"] == "Renamed"
    # Slug is untouched (lock-after-set).
    assert r.json()["slug"] == "p1"


@pytest.mark.asyncio
async def test_patch_agent_slug_rejected(client, auth_headers):
    """§13 D3: slug is set-once-then-locked. PATCH with slug → 422."""
    a = await client.post(
        "/v1/agents", json=_agent_payload(slug="locked"), headers=auth_headers
    )
    aid = a.json()["id"]
    r = await client.patch(
        f"/v1/agents/{aid}",
        json={"slug": "renamed"},
        headers=auth_headers,
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_patch_agent_clear_webhook_url(client, auth_headers):
    a = await client.post(
        "/v1/agents",
        json=_agent_payload(
            slug="wh1",
            webhook_url="https://example.com/wh",
        ),
        headers=auth_headers,
    )
    assert a.json()["webhook_secret"] is not None
    aid = a.json()["id"]

    # Clear webhook_url via explicit null. Both URL and secret should drop.
    r = await client.patch(
        f"/v1/agents/{aid}",
        json={"webhook_url": None},
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert r.json()["webhook_url"] is None
    # Read endpoint never reveals secret, but the secret retrieval
    # endpoint should now 404.
    sec = await client.get(f"/v1/agents/{aid}/webhook-secret", headers=auth_headers)
    assert sec.status_code == 404


@pytest.mark.asyncio
async def test_patch_agent_set_webhook_mints_secret(client, auth_headers):
    a = await client.post(
        "/v1/agents", json=_agent_payload(slug="wh2"), headers=auth_headers
    )
    aid = a.json()["id"]
    r = await client.patch(
        f"/v1/agents/{aid}",
        json={"webhook_url": "https://example.com/wh"},
        headers=auth_headers,
    )
    assert r.status_code == 200
    sec = await client.get(f"/v1/agents/{aid}/webhook-secret", headers=auth_headers)
    assert sec.status_code == 200
    assert sec.json()["webhook_secret"].startswith("whsec_")


@pytest.mark.asyncio
async def test_delete_agent_soft_delete_then_404(client, auth_headers):
    a = await client.post(
        "/v1/agents", json=_agent_payload(slug="goodbye"), headers=auth_headers
    )
    aid = a.json()["id"]
    d = await client.delete(f"/v1/agents/{aid}", headers=auth_headers)
    assert d.status_code == 204
    # Default GET (no include_deleted) → 404.
    r = await client.get(f"/v1/agents/{aid}", headers=auth_headers)
    assert r.status_code == 404
    # With include_deleted → 200 and deleted_at populated.
    r2 = await client.get(f"/v1/agents/{aid}?include_deleted=true", headers=auth_headers)
    assert r2.status_code == 200
    assert r2.json()["deleted_at"] is not None


@pytest.mark.asyncio
async def test_regenerate_webhook_secret_requires_confirmation(client, auth_headers):
    a = await client.post(
        "/v1/agents",
        json=_agent_payload(
            slug="rotor",
            webhook_url="https://example.com/wh",
        ),
        headers=auth_headers,
    )
    original = a.json()["webhook_secret"]
    aid = a.json()["id"]
    # Without the confirmation header → 400.
    r = await client.post(
        f"/v1/agents/{aid}/webhook-secret/regenerate", headers=auth_headers
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "confirmation_required"
    # With confirmation header → 200 and a new secret.
    r2 = await client.post(
        f"/v1/agents/{aid}/webhook-secret/regenerate",
        headers={**auth_headers, "X-Confirm-Destructive": "true"},
    )
    assert r2.status_code == 200
    new_secret = r2.json()["webhook_secret"]
    assert new_secret.startswith("whsec_")
    assert new_secret != original


@pytest.mark.asyncio
async def test_regenerate_secret_without_webhook_url_409(client, auth_headers):
    a = await client.post(
        "/v1/agents", json=_agent_payload(slug="no-wh"), headers=auth_headers
    )
    aid = a.json()["id"]
    r = await client.post(
        f"/v1/agents/{aid}/webhook-secret/regenerate",
        headers={**auth_headers, "X-Confirm-Destructive": "true"},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "no_webhook_url"


@pytest.mark.asyncio
async def test_create_rejects_unknown_field(client, auth_headers):
    """Pydantic extra='forbid' on AgentCreate."""
    r = await client.post(
        "/v1/agents",
        json={"display_name": "X", "bogus_field": "y"},
        headers=auth_headers,
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_create_invalid_slug_format(client, auth_headers):
    """Slug regex: lowercase alphanumeric + hyphens, no leading/trailing hyphen."""
    for bad in ["-leading", "trailing-", "UPPER", "with space"]:
        r = await client.post(
            "/v1/agents",
            json=_agent_payload(slug=bad),
            headers=auth_headers,
        )
        assert r.status_code == 422, f"slug {bad!r} should have been rejected"
