"""Agent (Identity) service layer.

Spec: `https://trydock.ai/mike/cueapi-messaging-primitive-v1-sp` §2 (Identity primitive) +
§6 (slug-form addressing).

Owns all CRUD plus the slug-form address resolver used by the message
router (§3.3 step 2). Webhook URL validation reuses the existing
``validate_callback_url`` SSRF defense from cue + alert + memory-block
infrastructure.

Public API:

* ``resolve_address(db, addr)`` — opaque ``agt_xxx`` OR slug-form
  ``agent_slug@user_slug``. Single resolver used by every code path
  that accepts a public agent reference. Returns the live (non-deleted)
  Agent row or raises 404 / 400.
* ``create_agent`` / ``list_agents`` / ``get_agent`` / ``update_agent``
  / ``soft_delete_agent`` — CRUD primitives.
* ``rotate_webhook_secret`` — generates a fresh ``whsec_<64 hex>``
  and instantly invalidates the old one. Old secret is dropped from
  the row immediately; in-flight push deliveries that captured the
  prior secret will succeed (server-side dispatch reads the live
  secret per dispatch attempt — see §5.1 callout).

Reserved slugs are rejected at create time per §6.5: ``admin``,
``api``, ``auth``, ``billing``, ``system``, ``support``. These match
the public-API path-segment patterns most likely to confuse a caller
that constructs URLs naively.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthenticatedUser
from app.config import settings
from app.models import Agent, User
from app.utils.ids import generate_agent_id, generate_webhook_secret
from app.utils.slug import derive_agent_slug
from app.utils.url_validation import validate_callback_url

RESERVED_SLUGS = frozenset({
    "admin",
    "api",
    "auth",
    "billing",
    "system",
    "support",
})

OPAQUE_ID_LENGTH = 16  # "agt_" (4) + 12 alphanumeric


def _http_error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"error": {"code": code, "message": message, "status": status}},
    )


def _looks_like_opaque_id(addr: str) -> bool:
    return (
        len(addr) == OPAQUE_ID_LENGTH
        and addr.startswith("agt_")
        and addr[4:].isalnum()
    )


async def resolve_address(db: AsyncSession, addr: str) -> Agent:
    """Resolve a public agent address to a live Agent row.

    Accepts opaque ``agt_<12 alphanum>`` OR slug-form
    ``agent_slug@user_slug``. Returns the agent. Raises 404 if missing
    or soft-deleted; 400 if the address is malformed.

    The order matters: opaque-form first because the regex is tighter
    and won't match slug-form by accident; slug-form second because it
    requires a join.
    """
    if _looks_like_opaque_id(addr):
        result = await db.execute(
            select(Agent).where(Agent.id == addr, Agent.deleted_at.is_(None))
        )
        agent = result.scalar_one_or_none()
        if not agent:
            raise _http_error(404, "agent_not_found", f"agent {addr} not found")
        return agent

    if "@" in addr:
        parts = addr.split("@", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise _http_error(
                400,
                "invalid_agent_address",
                "slug-form address must be 'agent_slug@user_slug'",
            )
        agent_slug, user_slug = parts
        result = await db.execute(
            select(Agent)
            .join(User, Agent.user_id == User.id)
            .where(
                User.slug == user_slug,
                Agent.slug == agent_slug,
                Agent.deleted_at.is_(None),
            )
        )
        agent = result.scalar_one_or_none()
        if not agent:
            raise _http_error(404, "agent_not_found", f"agent {addr} not found")
        return agent

    raise _http_error(
        400,
        "invalid_agent_address",
        "address must be opaque ID (agt_xxx) or slug-form (agent_slug@user_slug)",
    )


def _validate_slug_or_raise(slug: str) -> None:
    if slug in RESERVED_SLUGS:
        raise _http_error(
            400,
            "reserved_slug",
            f"slug '{slug}' is reserved",
        )


def _validate_webhook_url_or_raise(url: str) -> None:
    is_valid, err = validate_callback_url(url, settings.ENV)
    if not is_valid:
        raise _http_error(400, "invalid_callback_url", err)


async def create_agent(
    db: AsyncSession,
    user: AuthenticatedUser,
    *,
    slug: Optional[str],
    display_name: str,
    webhook_url: Optional[str],
    metadata: Dict,
) -> Tuple[Agent, Optional[str]]:
    """Create an agent. Returns (agent, plaintext_webhook_secret).

    The plaintext webhook secret is non-None only when a webhook_url
    was supplied. The caller (router) MUST surface the plaintext secret
    in the 201 response — it's stored on the row but the GET response
    omits it (only the dedicated retrieval endpoint reveals it).
    """
    if slug is not None:
        _validate_slug_or_raise(slug)
    if webhook_url:
        _validate_webhook_url_or_raise(webhook_url)

    final_slug = await derive_agent_slug(
        db, user.id, display_name, explicit=slug
    )
    if final_slug in RESERVED_SLUGS:
        # Auto-derived slug landed on a reserved name (`api` from
        # display_name='API'). Re-derive with a forced suffix.
        from uuid import uuid4
        final_slug = f"{final_slug[:59]}-{uuid4().hex[:4]}"

    plaintext_secret: Optional[str] = None
    if webhook_url:
        plaintext_secret = generate_webhook_secret()

    agent = Agent(
        id=generate_agent_id(),
        user_id=user.id,
        slug=final_slug,
        display_name=display_name,
        webhook_url=webhook_url,
        webhook_secret=plaintext_secret,
        metadata_=metadata or {},
        status="online",
    )
    db.add(agent)
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        # Most likely cause: concurrent slug collision against the
        # ``unique_user_agent_slug`` constraint.
        if "unique_user_agent_slug" in str(e.orig):
            raise _http_error(
                409,
                "slug_taken",
                f"slug '{final_slug}' already in use for this user",
            )
        raise

    await db.refresh(agent)
    return agent, plaintext_secret


async def list_agents(
    db: AsyncSession,
    user: AuthenticatedUser,
    *,
    status: Optional[str] = None,
    include_deleted: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> Dict:
    """List the caller's agents. Soft-deleted excluded by default."""
    base_filters = [Agent.user_id == user.id]
    if not include_deleted:
        base_filters.append(Agent.deleted_at.is_(None))
    if status is not None:
        base_filters.append(Agent.status == status)

    count_q = select(func.count()).select_from(Agent).where(*base_filters)
    total = (await db.execute(count_q)).scalar() or 0

    rows_q = (
        select(Agent)
        .where(*base_filters)
        .order_by(Agent.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await db.execute(rows_q)).scalars().all()
    return {"agents": list(rows), "total": int(total), "limit": limit, "offset": offset}


async def get_agent_owned(
    db: AsyncSession,
    user: AuthenticatedUser,
    addr: str,
    *,
    include_deleted: bool = False,
) -> Agent:
    """Fetch an agent the caller owns, by opaque ID or slug-form.

    Raises 404 if the agent doesn't exist OR if it isn't owned by the
    caller — the same code so we don't leak existence of other users'
    agents.
    """
    # We can't reuse resolve_address directly because it filters out
    # soft-deleted by default; the GET endpoint should be able to
    # surface soft-deleted records when ?include_deleted=true.
    if _looks_like_opaque_id(addr):
        q = select(Agent).where(Agent.id == addr, Agent.user_id == user.id)
    elif "@" in addr:
        parts = addr.split("@", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise _http_error(
                400,
                "invalid_agent_address",
                "slug-form address must be 'agent_slug@user_slug'",
            )
        agent_slug, user_slug = parts
        q = (
            select(Agent)
            .join(User, Agent.user_id == User.id)
            .where(
                User.slug == user_slug,
                Agent.slug == agent_slug,
                Agent.user_id == user.id,
            )
        )
    else:
        raise _http_error(
            400,
            "invalid_agent_address",
            "address must be opaque ID (agt_xxx) or slug-form (agent_slug@user_slug)",
        )

    if not include_deleted:
        q = q.where(Agent.deleted_at.is_(None))

    agent = (await db.execute(q)).scalar_one_or_none()
    if not agent:
        raise _http_error(404, "agent_not_found", f"agent {addr} not found")
    return agent


async def update_agent(
    db: AsyncSession,
    user: AuthenticatedUser,
    addr: str,
    *,
    display_name: Optional[str],
    webhook_url_set: bool,
    webhook_url: Optional[str],
    status: Optional[str],
    metadata: Optional[Dict],
) -> Agent:
    """Apply a partial update.

    ``webhook_url_set`` distinguishes "field omitted" (no change)
    from "field present and possibly null" (set to that value, with
    ``null`` clearing the URL and dropping the secret).
    """
    agent = await get_agent_owned(db, user, addr)

    if display_name is not None:
        agent.display_name = display_name
    if status is not None:
        agent.status = status
    if metadata is not None:
        agent.metadata_ = metadata

    if webhook_url_set:
        if webhook_url is None:
            agent.webhook_url = None
            agent.webhook_secret = None
        else:
            _validate_webhook_url_or_raise(webhook_url)
            agent.webhook_url = webhook_url
            if agent.webhook_secret is None:
                agent.webhook_secret = generate_webhook_secret()

    await db.commit()
    await db.refresh(agent)
    return agent


async def soft_delete_agent(
    db: AsyncSession,
    user: AuthenticatedUser,
    addr: str,
) -> None:
    agent = await get_agent_owned(db, user, addr)
    if agent.deleted_at is None:
        from datetime import datetime, timezone
        agent.deleted_at = datetime.now(timezone.utc)
        await db.commit()


async def rotate_webhook_secret(
    db: AsyncSession,
    user: AuthenticatedUser,
    addr: str,
) -> str:
    """Generate a new webhook secret. Returns the plaintext value.

    Caller must have set webhook_url first (we don't lazily mint a
    secret on rotation when there's no URL — that would leave the
    paired-constraint violated until the URL is set).
    """
    agent = await get_agent_owned(db, user, addr)
    if agent.webhook_url is None:
        raise _http_error(
            409,
            "no_webhook_url",
            "Set webhook_url before rotating the secret",
        )
    new_secret = generate_webhook_secret()
    agent.webhook_secret = new_secret
    await db.commit()
    return new_secret


async def get_webhook_secret(
    db: AsyncSession,
    user: AuthenticatedUser,
    addr: str,
) -> str:
    agent = await get_agent_owned(db, user, addr)
    if agent.webhook_secret is None:
        raise _http_error(
            404,
            "no_webhook_secret",
            "This agent has no webhook secret. Set webhook_url to mint one.",
        )
    return agent.webhook_secret


def to_response_dict(agent: Agent, *, include_secret: bool = False) -> Dict:
    """ORM Agent → dict suitable for AgentResponse.

    `metadata_` ORM attribute maps to ``metadata`` in the API surface.
    `webhook_secret` is only included when explicitly requested (POST
    response or webhook-secret rotation response).
    """
    return {
        "id": agent.id,
        "user_id": str(agent.user_id),
        "slug": agent.slug,
        "display_name": agent.display_name,
        "webhook_url": agent.webhook_url,
        "webhook_secret": agent.webhook_secret if include_secret else None,
        "metadata": agent.metadata_ or {},
        "status": agent.status,
        "deleted_at": agent.deleted_at,
        "created_at": agent.created_at,
        "updated_at": agent.updated_at,
    }
