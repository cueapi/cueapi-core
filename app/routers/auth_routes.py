from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthenticatedUser, get_current_user
from app.config import settings
from app.database import get_db
from app.models.cue import Cue
from app.models.user import User
from app.redis import get_redis
from app.services.usage_service import get_monthly_usage
from app.utils.ids import generate_api_key, generate_webhook_secret, get_api_key_prefix, hash_api_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    email: EmailStr


class RegisterResponse(BaseModel):
    api_key: str
    email: str


@router.post("/register", response_model=RegisterResponse, status_code=201)
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)):
    if not settings.ALLOW_REGISTER and settings.ENV != "development":
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "not_found", "message": "Not found", "status": 404}},
        )

    result = await db.execute(select(User).where(User.email == body.email))
    if result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=409,
            detail={"error": {"code": "duplicate_email", "message": "Email already registered", "status": 409}},
        )

    api_key = generate_api_key()

    user = User(
        email=body.email,
        api_key_hash=hash_api_key(api_key),
        api_key_prefix=get_api_key_prefix(api_key),
        webhook_secret=generate_webhook_secret(),
    )
    db.add(user)
    await db.commit()

    return RegisterResponse(api_key=api_key, email=body.email)


class RegenerateResponse(BaseModel):
    api_key: str
    previous_key_revoked: bool


@router.post("/key/regenerate", response_model=RegenerateResponse)
async def regenerate_key(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Regenerate API key. Old key is instantly revoked."""
    if request.headers.get("x-confirm-destructive") != "true":
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "confirmation_required", "message": "This action is destructive. Set X-Confirm-Destructive: true header to confirm.", "status": 400}},
        )

    result = await db.execute(select(User.api_key_hash).where(User.id == user.id))
    row = result.fetchone()
    old_hash = row.api_key_hash if row else None

    new_key = generate_api_key()
    new_hash = hash_api_key(new_key)
    new_prefix = get_api_key_prefix(new_key)

    from app.utils.session import encrypt_api_key
    try:
        new_encrypted = encrypt_api_key(new_key)
    except Exception:
        new_encrypted = None

    update_values = {
        "api_key_hash": new_hash,
        "api_key_prefix": new_prefix,
    }
    if new_encrypted:
        update_values["api_key_encrypted"] = new_encrypted
    await db.execute(
        update(User).where(User.id == user.id).values(**update_values)
    )
    await db.commit()

    redis = await get_redis()
    if old_hash:
        await redis.delete(f"auth:{old_hash}")
        await redis.set(f"rotated:{old_hash}", "1", ex=86400)

    from app.auth import AuthenticatedUser as AuthUser
    auth_user = AuthUser(
        id=user.id,
        email=user.email,
        plan=user.plan,
        active_cue_limit=user.active_cue_limit,
        monthly_execution_limit=user.monthly_execution_limit,
        rate_limit_per_minute=user.rate_limit_per_minute,
    )
    await redis.set(f"auth:{new_hash}", auth_user.model_dump_json(), ex=300)

    # Send email notification
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    _send_key_regeneration_email(user.email, timestamp)

    return RegenerateResponse(api_key=new_key, previous_key_revoked=True)


@router.get("/me")
async def get_me(
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return current user info with usage stats."""
    result = await db.execute(
        select(func.count()).select_from(Cue).where(
            Cue.user_id == user.id, Cue.status == "active"
        )
    )
    active_cues = result.scalar() or 0

    redis = await get_redis()
    executions_this_month = await get_monthly_usage(user.id, redis)

    ws_result = await db.execute(
        select(User.webhook_secret).where(User.id == user.id)
    )
    ws_row = ws_result.fetchone()
    has_webhook_secret = bool(ws_row and ws_row.webhook_secret)

    user_result = await db.execute(
        select(User.api_key_prefix).where(User.id == user.id)
    )
    user_row = user_result.fetchone()
    api_key_prefix = user_row.api_key_prefix if user_row else ""

    return {
        "email": user.email,
        "plan": user.plan,
        "active_cues": active_cues,
        "active_cue_limit": user.active_cue_limit,
        "executions_this_month": executions_this_month,
        "monthly_execution_limit": user.monthly_execution_limit,
        "rate_limit_per_minute": user.rate_limit_per_minute,
        "has_webhook_secret": has_webhook_secret,
        "api_key_prefix": api_key_prefix,
    }


class PatchMeRequest(BaseModel):
    email: Optional[str] = None


@router.patch("/me")
async def patch_me(
    body: PatchMeRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update user profile settings."""
    now = datetime.now(timezone.utc)
    updates = {"updated_at": now}
    if body.email is not None:
        updates["email"] = body.email
    if not updates or len(updates) == 1:  # only updated_at
        raise HTTPException(status_code=422, detail={"error": {"code": "no_fields", "message": "No fields to update", "status": 422}})
    await db.execute(update(User).where(User.id == user.id).values(**updates))
    await db.commit()
    return {"updated_at": now.isoformat()}


class SessionRequest(BaseModel):
    token: str


@router.post("/session")
async def create_session(
    body: SessionRequest,
    db: AsyncSession = Depends(get_db),
):
    """Exchange a one-time session token for a JWT session."""
    from app.models.device_code import DeviceCode
    from app.utils.session import create_session_jwt

    result = await db.execute(
        select(DeviceCode).where(
            DeviceCode.session_token == body.token,
            DeviceCode.status.in_(["approved", "claimed"]),
        )
    )
    dc = result.scalar_one_or_none()

    if not dc or not dc.user_id:
        raise HTTPException(
            status_code=401,
            detail={"error": {"code": "invalid_token", "message": "Invalid or expired session token", "status": 401}},
        )

    user_result = await db.execute(select(User).where(User.id == dc.user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=401,
            detail={"error": {"code": "user_not_found", "message": "User not found", "status": 401}},
        )

    await db.execute(
        update(DeviceCode)
        .where(DeviceCode.id == dc.id)
        .values(session_token=None)
    )
    await db.commit()

    session_jwt = create_session_jwt(str(user.id), user.email)

    return {
        "session_token": session_jwt,
        "email": user.email,
    }


@router.post("/session/refresh")
async def refresh_session(
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    """Issue a fresh session JWT with a new expiry."""
    from app.utils.session import create_session_jwt

    try:
        new_token = create_session_jwt(str(current_user.id), current_user.email)
    except RuntimeError as e:
        raise HTTPException(
            status_code=503,
            detail={"error": {"code": "session_unavailable", "message": str(e), "status": 503}},
        )
    return {"session_token": new_token, "email": current_user.email}


@router.get("/key")
async def reveal_key(
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Reveal the full API key. Requires authentication."""
    result = await db.execute(
        select(User.api_key_encrypted, User.api_key_prefix).where(User.id == user.id)
    )
    row = result.fetchone()

    if not row or not row.api_key_encrypted:
        return {
            "api_key": None,
            "prefix": row.api_key_prefix if row else "",
            "message": "Key not available. Regenerate to enable Reveal.",
        }

    from app.utils.session import decrypt_api_key
    try:
        decrypted = decrypt_api_key(row.api_key_encrypted)
    except Exception:
        return {
            "api_key": None,
            "prefix": row.api_key_prefix,
            "message": "Key decryption failed. Regenerate to fix.",
        }

    return {"api_key": decrypted}


def _send_key_regeneration_email(email: str, timestamp: str) -> None:
    """Send email notification when API key is regenerated (optional, requires resend)."""
    try:
        if settings.ENV == "development" or not settings.RESEND_API_KEY:
            logger.info("[DEV] API key regenerated for %s at %s", email, timestamp)
            return
        import resend
        resend.api_key = settings.RESEND_API_KEY
        from app.utils.templates import brand_email, email_button, email_code, email_heading, email_paragraph
        body_html = (
            email_paragraph(f"Your CueAPI API key was regenerated at <strong style='color:#ffffff;'>{timestamp}</strong>.")
            + email_paragraph("The old key is immediately invalid.")
            + email_heading("What to do now:")
            + email_paragraph(f"1. Copy your new key from the dashboard<br>2. Update your worker config<br>3. Restart your workers: {email_code('cueapi-worker start')}")
            + f'<p style="margin:24px 0;">{email_button("Open Dashboard", settings.BASE_URL)}</p>'
        )
        resend.Emails.send({
            "from": settings.RESEND_FROM_EMAIL,
            "to": [email],
            "subject": "[CueAPI] API key regenerated",
            "html": brand_email("API Key Regenerated", body_html),
        })
    except ImportError:
        logger.info("resend not installed — skipping key regeneration email for %s", email)
    except Exception as e:
        logger.error("Failed to send key regeneration email to %s: %s", email, e)


def _send_webhook_secret_regeneration_email(email: str, timestamp: str) -> None:
    """Send email notification when webhook secret is regenerated (optional, requires resend)."""
    try:
        if settings.ENV == "development" or not settings.RESEND_API_KEY:
            logger.info("[DEV] Webhook secret regenerated for %s at %s", email, timestamp)
            return
        import resend
        resend.api_key = settings.RESEND_API_KEY
        from app.utils.templates import brand_email, email_button, email_heading, email_paragraph
        body_html = (
            email_paragraph(f"Your CueAPI webhook signing secret was rotated at <strong style='color:#ffffff;'>{timestamp}</strong>.")
            + email_paragraph("The old secret is immediately invalid.")
            + email_heading("What to do now:")
            + email_paragraph("1. Copy your new secret from the dashboard<br>2. Update your webhook verification code")
            + f'<p style="margin:24px 0;">{email_button("Open Dashboard", settings.BASE_URL)}</p>'
        )
        resend.Emails.send({
            "from": settings.RESEND_FROM_EMAIL,
            "to": [email],
            "subject": "[CueAPI] Webhook secret rotated",
            "html": brand_email("Webhook Secret Rotated", body_html),
        })
    except ImportError:
        logger.info("resend not installed — skipping webhook secret email for %s", email)
    except Exception as e:
        logger.error("Failed to send webhook secret regeneration email to %s: %s", email, e)
