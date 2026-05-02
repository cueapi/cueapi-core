from __future__ import annotations

import logging

from arq.connections import RedisSettings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from worker.tasks import (
    deliver_message_task,
    deliver_webhook_task,
    retry_message_task,
    retry_webhook_task,
)

logger = logging.getLogger(__name__)


async def startup(ctx: dict):
    """Initialize DB engine and session factory for workers."""
    from app.utils.logging import setup_logging
    setup_logging()
    logger.info("Worker starting up...")
    engine = create_async_engine(
        settings.async_database_url,
        pool_size=settings.DATABASE_POOL_SIZE,
        max_overflow=settings.DATABASE_MAX_OVERFLOW,
    )
    ctx["db_engine"] = engine
    ctx["db_session_factory"] = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def shutdown(ctx: dict):
    """Clean up DB engine."""
    logger.info("Worker shutting down...")
    engine = ctx.get("db_engine")
    if engine:
        await engine.dispose()


class WorkerSettings:
    functions = [
        deliver_webhook_task,
        retry_webhook_task,
        deliver_message_task,
        retry_message_task,
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
    max_jobs = settings.WEBHOOK_CONCURRENCY_PER_WORKER
    job_timeout = settings.WEBHOOK_TIMEOUT_SECONDS + 10  # Give some buffer
