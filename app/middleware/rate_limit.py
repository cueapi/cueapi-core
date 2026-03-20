from __future__ import annotations

import json
import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.database import async_session as db_session_factory
from app.redis import get_redis
from app.services.usage_service import get_monthly_usage
from app.utils.ids import hash_api_key

logger = logging.getLogger(__name__)

# Paths exempt from rate limiting (exact match)
EXEMPT_PATHS = {"/health", "/status", "/docs", "/openapi.json", "/redoc", "/v1/billing/webhook", "/auth/device", "/v1/auth/verify", "/v1/internal/deploy-hook"}
# Path prefixes exempt from rate limiting (startswith match)
EXEMPT_PREFIXES = ("/v1/blog/", "/v1/internal/")

DEFAULT_RATE_LIMIT = 60


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Skip rate limiting for exempt paths
        path = request.url.path
        if path in EXEMPT_PATHS or path.startswith(EXEMPT_PREFIXES):
            return await call_next(request)

        # Try to get Redis — if unavailable, skip rate limiting entirely
        try:
            redis = await get_redis()
            await redis.ping()
        except Exception:
            logger.warning("Redis unavailable for rate limiting, allowing request")
            return await call_next(request)

        # Extract API key from auth header
        auth_header = request.headers.get("Authorization", "")
        rate_limit = DEFAULT_RATE_LIMIT
        ratelimit_key = None
        user_id = None
        monthly_limit = 0

        if auth_header.startswith("Bearer ") and "cue_sk_" in auth_header:
            api_key = auth_header.removeprefix("Bearer ").strip()
            key_hash = hash_api_key(api_key)
            ratelimit_key = f"ratelimit:{key_hash}"

            # Try to get user info from auth cache for tier-specific limit
            cache_key = f"auth:{key_hash}"
            try:
                cached = await redis.get(cache_key)
                if cached:
                    user_data = json.loads(cached)
                    rate_limit = user_data.get("rate_limit_per_minute", DEFAULT_RATE_LIMIT)
                    user_id = user_data.get("id")
                    monthly_limit = user_data.get("monthly_execution_limit", 0)
            except Exception:
                pass  # Redis error mid-check — continue with defaults

        if ratelimit_key is None:
            # No auth — rate limit by IP
            client_ip = request.client.host if request.client else "unknown"
            ratelimit_key = f"ratelimit:ip:{client_ip}"

        # Sliding window using sorted set
        # Check count FIRST, only add entry if under limit to prevent
        # rejected requests from inflating the window (feedback loop)
        try:
            now = time.time()
            window_start = now - 60

            pipe = redis.pipeline()
            pipe.zremrangebyscore(ratelimit_key, 0, window_start)
            pipe.zcard(ratelimit_key)
            results = await pipe.execute()

            current_count = results[1]
        except Exception:
            logger.warning("Redis error during rate limit check, allowing request")
            return await call_next(request)

        if current_count >= rate_limit:
            # Calculate retry-after (time until oldest entry expires)
            try:
                oldest = await redis.zrange(ratelimit_key, 0, 0, withscores=True)
                retry_after = 60
                reset_at = int(now + 60)
                if oldest:
                    retry_after = max(1, int(60 - (now - oldest[0][1])))
                    reset_at = int(oldest[0][1] + 60)
            except Exception:
                retry_after = 60
                reset_at = int(now + 60)

            logger.warning(
                "Rate limit exceeded",
                extra={
                    "event_type": "rate_limit_exceeded",
                    "ratelimit_key": ratelimit_key,
                    "current_count": current_count,
                    "rate_limit": rate_limit,
                    "retry_after": retry_after,
                    "path": request.url.path,
                },
            )

            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": "rate_limit_exceeded",
                        "message": f"Too many requests. Retry after {retry_after} seconds.",
                        "status": 429,
                    }
                },
                headers={
                    "X-RateLimit-Limit": str(rate_limit),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(reset_at),
                    "Retry-After": str(retry_after),
                },
            )

        # Request is allowed — NOW record it in the sliding window
        try:
            pipe = redis.pipeline()
            # Use unique member to prevent overwrites during burst traffic
            pipe.zadd(ratelimit_key, {f"{now}:{uuid.uuid4().hex[:8]}": now})
            pipe.expire(ratelimit_key, 70)  # TTL slightly > window
            await pipe.execute()
            current_count += 1  # reflect the just-added entry
        except Exception:
            pass  # Entry not recorded, that's OK — request still proceeds

        # Process request
        response = await call_next(request)

        # Add rate limit headers
        remaining = max(0, rate_limit - current_count)
        reset_at = int(now + 60)  # Window resets 60s from now
        response.headers["X-RateLimit-Limit"] = str(rate_limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(reset_at)

        # Add usage warning header if authenticated
        if user_id and monthly_limit > 0:
            try:
                async with db_session_factory() as db:
                    usage = await get_monthly_usage(user_id, redis, db)
                pct = usage / monthly_limit
                if pct >= 0.8:
                    response.headers["X-CueAPI-Usage-Warning"] = "approaching_limit"
            except Exception:
                pass  # Don't break requests over usage check failures

        return response
