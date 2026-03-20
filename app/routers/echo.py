from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth import AuthenticatedUser, get_current_user
from app.redis import get_redis
from app.utils.auth_rate_limit import check_auth_rate_limit

router = APIRouter(prefix="/v1/echo", tags=["echo"])

ECHO_TTL_SECONDS = 300  # 5 minutes
ECHO_MAX_PAYLOAD = 1_000_000  # 1MB


@router.post("/{token}")
async def echo_store(token: str, request: Request):
    """Store a payload keyed by token. No auth required. Expires after 5 minutes."""
    if len(token) < 16:
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "invalid_token", "message": "Token must be at least 16 characters", "status": 400}},
        )

    body = await request.body()
    if len(body) > ECHO_MAX_PAYLOAD:
        raise HTTPException(
            status_code=413,
            detail={"error": {"code": "payload_too_large", "message": "Echo payload must be under 1MB", "status": 413}},
        )

    redis = await get_redis()

    # Rate limit: 10 stores per IP per hour
    client_ip = request.client.host if request.client else "unknown"
    allowed = await check_auth_rate_limit(redis, f"echo_rl:{client_ip}", 10, 3600)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail={"error": {"code": "rate_limit_exceeded", "message": "Echo store rate limit exceeded", "status": 429}},
        )

    payload = json.loads(body)

    data = json.dumps({
        "payload": payload,
        "received_at": datetime.now(timezone.utc).isoformat(),
    })
    await redis.set(f"echo:{token}", data, ex=ECHO_TTL_SECONDS)

    return {"stored": True}


@router.get("/{token}")
async def echo_retrieve(
    token: str,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Retrieve a stored payload. Auth required."""
    redis = await get_redis()
    data = await redis.get(f"echo:{token}")

    if not data:
        return {"status": "waiting"}

    parsed = json.loads(data)
    return {
        "status": "delivered",
        "payload": parsed["payload"],
        "received_at": parsed["received_at"],
    }
