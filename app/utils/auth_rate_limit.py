from __future__ import annotations


async def check_auth_rate_limit(redis, key: str, limit: int, window_seconds: int) -> bool:
    """Simple counter-based rate limit for auth endpoints.

    Returns True if allowed, False if blocked.
    """
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, window_seconds)
    return count <= limit
