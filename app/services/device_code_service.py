from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.device_code import DeviceCode
from app.models.user import User
from app.utils.ids import generate_api_key, generate_webhook_secret, get_api_key_prefix, hash_api_key
from app.utils.templates import brand_email, email_button, email_paragraph

logger = logging.getLogger(__name__)

DEVICE_CODE_EXPIRY_SECONDS = 900  # 15 minutes


async def create_device_code(db: AsyncSession, device_code: str) -> dict:
    """Create a pending device code row."""
    # Check if device_code already exists and is not expired
    result = await db.execute(
        select(DeviceCode).where(DeviceCode.device_code == device_code)
    )
    existing = result.scalar_one_or_none()
    if existing:
        if existing.expires_at > datetime.now(timezone.utc) and existing.status not in ("expired", "claimed"):
            return {"error": {"code": "device_code_exists", "message": "Device code already in use", "status": 409}}
        # Expired or claimed — delete old row so we can reuse the code
        await db.delete(existing)
        await db.flush()

    dc = DeviceCode(
        device_code=device_code,
        status="pending",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=DEVICE_CODE_EXPIRY_SECONDS),
    )
    db.add(dc)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return {"error": {"code": "device_code_exists", "message": "Device code already in use", "status": 409}}

    base_url = settings.BASE_URL.rstrip("/")
    verification_url = f"{base_url}/auth/device?code={device_code}"

    return {
        "verification_url": verification_url,
        "expires_in": DEVICE_CODE_EXPIRY_SECONDS,
    }


async def poll_device_code(db: AsyncSession, device_code: str) -> dict:
    """Poll the status of a device code."""
    result = await db.execute(
        select(DeviceCode).where(DeviceCode.device_code == device_code)
    )
    dc = result.scalar_one_or_none()

    if not dc:
        return {"status": "expired"}

    # Check expiry
    if dc.expires_at < datetime.now(timezone.utc):
        if dc.status not in ("expired", "claimed", "approved"):
            await db.execute(
                update(DeviceCode)
                .where(DeviceCode.id == dc.id)
                .values(status="expired")
            )
            await db.commit()
        return {"status": "expired"}

    if dc.status == "approved":
        session_token = dc.session_token
        if dc.api_key_plaintext:
            # New user — return the key and immediately mark as claimed
            api_key = dc.api_key_plaintext
            email = dc.email
            await db.execute(
                update(DeviceCode)
                .where(DeviceCode.id == dc.id)
                .values(status="claimed", api_key_plaintext=None)
            )
            await db.commit()
            resp = {"status": "approved", "api_key": api_key, "email": email}
            if session_token:
                resp["session_token"] = session_token
            return resp
        else:
            # Existing user — try to return decrypted key if available
            email = dc.email
            await db.execute(
                update(DeviceCode)
                .where(DeviceCode.id == dc.id)
                .values(status="claimed")
            )
            await db.commit()
            resp = {"status": "approved", "email": email}
            if session_token:
                resp["session_token"] = session_token

            # Try to return the existing API key (if encrypted version exists)
            if dc.user_id:
                user_result = await db.execute(
                    select(User.api_key_encrypted).where(User.id == dc.user_id)
                )
                user_row = user_result.fetchone()
                if user_row and user_row.api_key_encrypted:
                    try:
                        from app.utils.session import decrypt_api_key
                        resp["api_key"] = decrypt_api_key(user_row.api_key_encrypted)
                    except Exception:
                        resp["existing_user"] = True
                else:
                    resp["existing_user"] = True
            else:
                resp["existing_user"] = True
            return resp

    return {"status": dc.status}


async def submit_email(db: AsyncSession, device_code: str, email: str) -> dict:
    """Submit email for a device code, send magic link."""
    result = await db.execute(
        select(DeviceCode).where(DeviceCode.device_code == device_code)
    )
    dc = result.scalar_one_or_none()

    if not dc:
        return {"error": {"code": "device_code_not_found", "message": "Device code not found", "status": 404}}

    if dc.expires_at < datetime.now(timezone.utc):
        return {"error": {"code": "device_code_expired", "message": "Device code expired", "status": 400}}

    if dc.status != "pending":
        return {"error": {"code": "invalid_status", "message": f"Device code status is '{dc.status}', expected 'pending'", "status": 400}}

    # Generate verification token
    verification_token = secrets.token_hex(32)

    await db.execute(
        update(DeviceCode)
        .where(DeviceCode.id == dc.id)
        .values(
            email=email,
            verification_token=verification_token,
            status="email_sent",
        )
    )
    await db.commit()

    # Send magic link
    base_url = settings.BASE_URL.rstrip("/")
    magic_link = f"{base_url}/v1/auth/verify?token={verification_token}&device_code={device_code}"

    if settings.ENV == "development" or not settings.RESEND_API_KEY:
        logger.info(f"[DEV] Magic link for {email}: {magic_link}")
        # Also print to stdout for easy testing
        print(f"\n{'='*60}")
        print(f"MAGIC LINK for {email}:")
        print(f"{magic_link}")
        print(f"{'='*60}\n")
    else:
        # Send via Resend (if installed)
        try:
            import resend
            resend.api_key = settings.RESEND_API_KEY
            body_html = (
                email_paragraph("Click the button below to complete your sign-in.")
                + f'<p style="margin:24px 0;">{email_button("Sign in to CueAPI", magic_link)}</p>'
                + email_paragraph(
                    "This link expires in 15 minutes. If you didn't request "
                    "this, you can safely ignore this email."
                )
            )
            resend.Emails.send({
                "from": settings.RESEND_FROM_EMAIL,
                "to": [email],
                "subject": "Sign in to CueAPI",
                "html": brand_email("Sign in to CueAPI", body_html),
            })
            logger.info(
                "Magic link email sent",
                extra={"event_type": "magic_link_sent", "email": email},
            )
        except ImportError:
            logger.warning("resend package not installed — magic link email not sent for %s", email)
        except Exception as e:
            logger.error(
                f"Failed to send magic link email: {e}",
                extra={"event_type": "magic_link_failed", "email": email},
            )
            # Still return email_sent — the device code status is already updated
            # User can retry by creating a new device code

    return {"status": "email_sent"}


async def verify_token(db: AsyncSession, token: str, device_code: str, redis) -> dict:
    """Verify magic link token, create/load user, generate API key."""
    result = await db.execute(
        select(DeviceCode).where(DeviceCode.device_code == device_code)
    )
    dc = result.scalar_one_or_none()

    if not dc:
        return {"error": "Device code not found"}

    if dc.expires_at < datetime.now(timezone.utc):
        return {"error": "Device code expired"}

    if dc.status not in ("email_sent",):
        return {"error": "Invalid device code status"}

    if dc.verification_token != token:
        return {"error": "Invalid verification token"}

    email = dc.email

    # Look up existing user
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    # Generate one-time session token for dashboard exchange
    one_time_session_token = secrets.token_hex(32)

    if user:
        # Existing user — DO NOT regenerate API key.
        # Login should never rotate a working key.
        logger.info(
            "Existing user verified via magic link (key NOT rotated)",
            extra={"event_type": "device_code_existing_user", "email": email},
        )
        await db.execute(
            update(DeviceCode)
            .where(DeviceCode.id == dc.id)
            .values(
                api_key_plaintext=None,
                status="approved",
                verification_token=None,
                session_token=one_time_session_token,
                user_id=user.id,
            )
        )
        await db.commit()
        return {
            "success": True,
            "existing_user": True,
            "email": email,
            "session_token": one_time_session_token,
        }
    else:
        # New user — create account and generate API key
        new_api_key = generate_api_key()
        new_hash = hash_api_key(new_api_key)
        new_prefix = get_api_key_prefix(new_api_key)

        # Encrypt the key for persistent storage
        new_encrypted = None
        try:
            from app.utils.session import encrypt_api_key
            new_encrypted = encrypt_api_key(new_api_key)
        except Exception:
            logger.debug("SESSION_SECRET not configured, skipping key encryption")

        user = User(
            email=email,
            api_key_hash=new_hash,
            api_key_prefix=new_prefix,
            webhook_secret=generate_webhook_secret(),
            api_key_encrypted=new_encrypted,
        )
        db.add(user)
        await db.flush()  # Get user.id

        # Store plaintext key so the poll endpoint can deliver it
        await db.execute(
            update(DeviceCode)
            .where(DeviceCode.id == dc.id)
            .values(
                api_key_plaintext=new_api_key,
                status="approved",
                verification_token=None,
                session_token=one_time_session_token,
                user_id=user.id,
            )
        )
        await db.commit()
        return {
            "success": True,
            "existing_user": False,
            "email": email,
            "api_key": new_api_key,
            "session_token": one_time_session_token,
        }
