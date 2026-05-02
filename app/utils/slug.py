"""Slug derivation for the messaging primitive's slug-form addressing.

User slugs are auto-derived from the email-local-part on registration:
lower-cased, non-alphanumerics replaced with hyphens, leading/trailing
hyphens trimmed, empty fallback to ``"user"``. On collision against an
existing user_slug, a short uuid-hex suffix is appended.

Lock-after-set means the slug is settable on registration and patchable
once via ``PATCH /v1/auth/me`` (when the column is NULL or matches the
auto-derived value), then locked. v1 doesn't enforce the lock-after-set
in this util — the PATCH endpoint owns that policy.

For agent slugs, the analogous derivation runs over ``display_name``
when the caller doesn't supply an explicit slug; same hyphenation +
fallback + collision-suffix logic, scoped to the agent's owning user.
"""
from __future__ import annotations

import re
import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


_NON_SLUG_CHARS = re.compile(r"[^a-z0-9]+")
_HYPHEN_RUN = re.compile(r"-+")


def _normalize(text: str, *, fallback: str = "user") -> str:
    """Normalize a string into a slug-shaped value (no uniqueness check)."""
    s = _NON_SLUG_CHARS.sub("-", (text or "").lower())
    s = _HYPHEN_RUN.sub("-", s).strip("-")
    return s or fallback


async def derive_user_slug(
    db: AsyncSession,
    email: str,
    *,
    max_attempts: int = 5,
) -> str:
    """Return a unique users.slug derived from the email-local-part.

    Derivation: lower-case, non-alphanumerics → hyphens, trim, fallback
    to ``"user"`` if empty. On collision (slug already taken), appends
    a 4-char uuid-hex suffix and retries up to ``max_attempts`` times.
    Final fallback is an 8-char suffix (vanishingly unlikely to collide
    in practice; the unique constraint at the DB layer would catch any
    residual race).

    Caller is responsible for handling ``IntegrityError`` if a concurrent
    registration grabs the same slug between this query and the INSERT.
    """
    from app.models import User

    local = email.split("@", 1)[0]
    base = _normalize(local)
    # Cap base at 60 chars so we have room for a "-XXXX" 5-char suffix
    # within the 64-char column limit.
    base = base[:60]

    # Try the bare base first.
    existing = await db.execute(select(User).where(User.slug == base).limit(1))
    if existing.scalar_one_or_none() is None:
        return base

    # Collision — append 4-char uuid-hex suffix.
    for _ in range(max_attempts):
        candidate = f"{base[:59]}-{uuid.uuid4().hex[:4]}"
        existing = await db.execute(select(User).where(User.slug == candidate).limit(1))
        if existing.scalar_one_or_none() is None:
            return candidate

    # Vanishingly unlikely: 5 collisions. Fall back to 8-char suffix.
    return f"{base[:55]}-{uuid.uuid4().hex[:8]}"


async def derive_agent_slug(
    db: AsyncSession,
    user_id,
    display_name: str,
    *,
    explicit: Optional[str] = None,
    max_attempts: int = 5,
) -> str:
    """Return a per-user-unique agents.slug.

    If ``explicit`` is supplied (caller provided ``slug`` on
    ``POST /v1/agents``), validate format and return as-is — caller
    is responsible for catching uniqueness conflicts via IntegrityError
    and surfacing to the user as a slug-already-taken error.

    If ``explicit`` is None, derive from ``display_name`` (same shape as
    user-slug derivation: lower, alphanumeric+hyphens, fallback "agent").
    On per-user collision, append uuid-hex suffix.
    """
    from app.models import Agent

    if explicit is not None:
        return explicit

    base = _normalize(display_name, fallback="agent")[:60]

    existing = await db.execute(
        select(Agent).where(Agent.user_id == user_id, Agent.slug == base).limit(1)
    )
    if existing.scalar_one_or_none() is None:
        return base

    for _ in range(max_attempts):
        candidate = f"{base[:59]}-{uuid.uuid4().hex[:4]}"
        existing = await db.execute(
            select(Agent).where(Agent.user_id == user_id, Agent.slug == candidate).limit(1)
        )
        if existing.scalar_one_or_none() is None:
            return candidate

    return f"{base[:55]}-{uuid.uuid4().hex[:8]}"
