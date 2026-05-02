"""Internal user-upsert endpoint for external auth-backend integrators (PR-5c).

When ``EXTERNAL_AUTH_BACKEND=True`` is set, the integrator's identity
system is the source of truth for users. They mirror their User table
into Cue's via this endpoint at signup / role-change / deletion time.

This endpoint is auth-gated by the shared ``INTERNAL_AUTH_TOKEN`` —
not the per-user API-key model. Mounting is conditional on
``EXTERNAL_AUTH_BACKEND=True`` (see app/main.py); when the flag is
False the route does not appear at all.

Shape mirrors the messaging-spec §3.1 of the cueapi-port PRD on Dock:

    PUT /v1/internal/users/{user_id}
    Authorization: Bearer <INTERNAL_AUTH_TOKEN>
    {
      "email": "user@example.com",
      "slug": "user-slug",
      "plan": "free" | "pro" | "scale" | "enterprise",
      "active_cue_limit": 10,
      "monthly_execution_limit": 300,
      "monthly_message_limit": 300,
      "rate_limit_per_minute": 60
    }

Idempotent: re-issuing the same body is a no-op. Issuing with
different values updates the existing row.

The integrator owns identity lifecycle — there is no DELETE here in
PR-5c. Soft-delete via the integrator setting plan='deleted' or
similar is the v1 pattern; cascade-delete via the FK chain on
``users.id`` handles agent / message tear-down at the integrator's
discretion. A future PR can add a typed delete endpoint if integrator
demand surfaces.
"""
from __future__ import annotations

import hmac
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.user import User
from app.utils.ids import generate_api_key, generate_webhook_secret, get_api_key_prefix, hash_api_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/internal", tags=["internal"])


class UserUpsertRequest(BaseModel):
    email: EmailStr
    slug: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$")
    plan: Optional[str] = Field(None, max_length=20)
    active_cue_limit: Optional[int] = Field(None, ge=0)
    monthly_execution_limit: Optional[int] = Field(None, ge=0)
    monthly_message_limit: Optional[int] = Field(None, ge=0)
    rate_limit_per_minute: Optional[int] = Field(None, ge=0)


class UserUpsertResponse(BaseModel):
    id: str
    email: str
    slug: str
    plan: str
    active_cue_limit: int
    monthly_execution_limit: int
    monthly_message_limit: int
    rate_limit_per_minute: int
    created: bool
    """True if a new row was inserted; False if an existing row was updated."""


def _require_internal_token(request: Request) -> None:
    """Auth: only requests bearing the shared INTERNAL_AUTH_TOKEN may
    call this. Constant-time compare. The standard ``get_current_user``
    dependency is intentionally NOT used here — the internal-user
    endpoints predate user identity (they CREATE the identity), so a
    User-keyed auth check would chicken-and-egg.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail={"error": {"code": "invalid_internal_token", "message": "Missing or invalid Authorization header", "status": 401}},
        )
    token = auth_header.removeprefix("Bearer ").strip()
    if not settings.INTERNAL_AUTH_TOKEN or not hmac.compare_digest(token, settings.INTERNAL_AUTH_TOKEN):
        raise HTTPException(
            status_code=401,
            detail={"error": {"code": "invalid_internal_token", "message": "Bearer token does not match INTERNAL_AUTH_TOKEN", "status": 401}},
        )


@router.put("/users/{user_id}", response_model=UserUpsertResponse)
async def upsert_user(
    user_id: str,
    body: UserUpsertRequest,
    request: Request,
):
    """Upsert a user row by UUID. Idempotent."""
    _require_internal_token(request)

    try:
        target_id = uuid.UUID(user_id)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "invalid_user_id", "message": "user_id must be a UUID", "status": 400}},
        )

    db_gen = get_db()
    db: AsyncSession = await db_gen.__anext__()
    try:
        result = await db.execute(select(User).where(User.id == target_id))
        user = result.scalar_one_or_none()
        created = user is None

        if created:
            # New row — synthesize a stub api_key (not used in
            # external-auth mode; satisfies NOT NULL constraint).
            raw_key = generate_api_key()
            user = User(
                id=target_id,
                email=body.email,
                slug=body.slug,
                api_key_hash=hash_api_key(raw_key),
                api_key_prefix=get_api_key_prefix(raw_key),
                webhook_secret=generate_webhook_secret(),
                plan=body.plan or "free",
                active_cue_limit=body.active_cue_limit if body.active_cue_limit is not None else 10,
                monthly_execution_limit=body.monthly_execution_limit if body.monthly_execution_limit is not None else 300,
                monthly_message_limit=body.monthly_message_limit if body.monthly_message_limit is not None else 300,
                rate_limit_per_minute=body.rate_limit_per_minute if body.rate_limit_per_minute is not None else 60,
            )
            db.add(user)
        else:
            # Update path — only overwrite fields the caller explicitly
            # set. Other fields (api_key_hash, etc.) untouched.
            user.email = body.email
            user.slug = body.slug
            if body.plan is not None:
                user.plan = body.plan
            if body.active_cue_limit is not None:
                user.active_cue_limit = body.active_cue_limit
            if body.monthly_execution_limit is not None:
                user.monthly_execution_limit = body.monthly_execution_limit
            if body.monthly_message_limit is not None:
                user.monthly_message_limit = body.monthly_message_limit
            if body.rate_limit_per_minute is not None:
                user.rate_limit_per_minute = body.rate_limit_per_minute

        await db.commit()
        await db.refresh(user)

        logger.info(
            "Internal user %s",
            "created" if created else "updated",
            extra={
                "event_type": "internal_user_upsert",
                "user_id": str(user.id),
                "created": created,
            },
        )

        return UserUpsertResponse(
            id=str(user.id),
            email=user.email,
            slug=user.slug,
            plan=user.plan,
            active_cue_limit=user.active_cue_limit,
            monthly_execution_limit=user.monthly_execution_limit,
            monthly_message_limit=user.monthly_message_limit,
            rate_limit_per_minute=user.rate_limit_per_minute,
            created=created,
        )
    finally:
        try:
            await db_gen.aclose()
        except Exception:
            pass
