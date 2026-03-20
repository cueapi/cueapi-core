from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthenticatedUser, get_current_user
from app.database import get_db
from app.redis import get_redis
from app.services.usage_service import get_usage_stats
from app.utils.ids import hash_api_key

router = APIRouter(prefix="/v1", tags=["usage"])


@router.get("/usage")
async def usage(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    redis = await get_redis()

    # Extract rate limit key from Authorization header
    ratelimit_key = None
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer ") and "cue_sk_" in auth_header:
        api_key = auth_header.removeprefix("Bearer ").strip()
        key_hash = hash_api_key(api_key)
        ratelimit_key = f"ratelimit:{key_hash}"

    return await get_usage_stats(user.id, redis, db, user, ratelimit_key=ratelimit_key)
