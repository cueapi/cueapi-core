from __future__ import annotations

import json
import logging

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.redis import get_redis
from app.utils.ids import hash_api_key

logger = logging.getLogger(__name__)


class AuthenticatedUser(BaseModel):
    id: str
    email: str
    plan: str
    active_cue_limit: int
    monthly_execution_limit: int
    rate_limit_per_minute: int


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> AuthenticatedUser:
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail={"error": {"code": "invalid_api_key", "message": "Missing or invalid Authorization header", "status": 401}},
        )

    token = auth_header.removeprefix("Bearer ").strip()

    if token.startswith("cue_sk_"):
        return await _auth_via_api_key(token, db)
    else:
        return await _auth_via_session_jwt(token, db)


async def _auth_via_api_key(api_key: str, db: AsyncSession) -> AuthenticatedUser:
    """Authenticate via API key."""
    key_hash = hash_api_key(api_key)
    cache_key = f"auth:{key_hash}"

    # Try Redis cache (graceful degradation)
    cached_user = None
    try:
        redis = await get_redis()
        cached = await redis.get(cache_key)
        if cached:
            cached_user = AuthenticatedUser(**json.loads(cached))
    except Exception:
        logger.warning("Redis unavailable for auth cache, falling back to DB")

    if cached_user:
        return cached_user

    # Fallback to DB
    result = await db.execute(select(User).where(User.api_key_hash == key_hash))
    user = result.scalar_one_or_none()
    if user is None:
        error_code = "invalid_api_key"
        error_message = "Invalid API key"
        try:
            redis = await get_redis()
            is_rotated = await redis.get(f"rotated:{key_hash}")
            if is_rotated:
                error_code = "key_rotated"
                error_message = "This API key has been rotated. Use your new key."
        except Exception:
            pass
        raise HTTPException(
            status_code=401,
            detail={"error": {"code": error_code, "message": error_message, "status": 401}},
        )

    auth_user = AuthenticatedUser(
        id=str(user.id),
        email=user.email,
        plan=user.plan,
        active_cue_limit=user.active_cue_limit,
        monthly_execution_limit=user.monthly_execution_limit,
        rate_limit_per_minute=user.rate_limit_per_minute,
    )

    # Cache in Redis for 5 minutes (best effort)
    try:
        redis = await get_redis()
        await redis.set(cache_key, auth_user.model_dump_json(), ex=300)
    except Exception:
        logger.warning("Redis unavailable for auth cache write")

    return auth_user


async def _auth_via_session_jwt(token: str, db: AsyncSession) -> AuthenticatedUser:
    """Authenticate via session JWT."""
    import jwt
    try:
        from app.utils.session import decode_session_jwt
        claims = decode_session_jwt(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=401,
            detail={"error": {"code": "session_expired", "message": "Session expired. Please log in again.", "status": 401}},
        )
    except (jwt.InvalidTokenError, Exception):
        raise HTTPException(
            status_code=401,
            detail={"error": {"code": "invalid_session", "message": "Invalid session token", "status": 401}},
        )

    user_id = claims["sub"]
    cache_key = f"session:{user_id}"

    try:
        redis = await get_redis()
        cached = await redis.get(cache_key)
        if cached:
            return AuthenticatedUser(**json.loads(cached))
    except Exception:
        logger.warning("Redis unavailable for session cache, falling back to DB")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=401,
            detail={"error": {"code": "user_not_found", "message": "User not found", "status": 401}},
        )

    auth_user = AuthenticatedUser(
        id=str(user.id),
        email=user.email,
        plan=user.plan,
        active_cue_limit=user.active_cue_limit,
        monthly_execution_limit=user.monthly_execution_limit,
        rate_limit_per_minute=user.rate_limit_per_minute,
    )

    try:
        redis = await get_redis()
        await redis.set(cache_key, auth_user.model_dump_json(), ex=300)
    except Exception:
        logger.warning("Redis unavailable for session cache write")

    return auth_user
