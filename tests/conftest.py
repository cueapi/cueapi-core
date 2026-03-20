from __future__ import annotations

import asyncio
import os
import uuid

# Ensure SESSION_SECRET is set for tests (must be before settings import)
os.environ.setdefault("SESSION_SECRET", "test-session-secret-32-chars-minimum!!")

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

# Ensure SESSION_SECRET is available on the settings object for tests
if not settings.SESSION_SECRET:
    settings.SESSION_SECRET = "test-session-secret-32-chars-minimum!!"

from app.database import Base, get_db
from app.main import app
from app.models import Cue, DispatchOutbox, Execution, UsageMonthly, User, Worker, DeviceCode  # noqa: F401

# Use the same database but create/drop tables for isolation
TEST_DATABASE_URL = settings.DATABASE_URL

engine = create_async_engine(TEST_DATABASE_URL, pool_size=5, max_overflow=5)
test_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(autouse=True)
async def setup_database():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture(autouse=True)
async def flush_rate_limits():
    """Flush all rate limit keys before each test to prevent pollution."""
    client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    for pattern in ["ratelimit:*", "auth_dc:*", "auth_ml:*", "auth_ml_ip:*", "auth_poll:*", "echo_rl:*", "support_rl:*", "session:*", "backfill:*", "auth:*"]:
        keys = await client.keys(pattern)
        if keys:
            await client.delete(*keys)
    yield
    if hasattr(client, "aclose"):
        await client.aclose()
    else:
        await client.close()


@pytest_asyncio.fixture
async def db_session():
    async with test_session() as session:
        yield session


async def override_get_db():
    async with test_session() as session:
        yield session


app.dependency_overrides[get_db] = override_get_db


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def registered_user(client: AsyncClient):
    email = f"user-{uuid.uuid4().hex[:8]}@test.com"
    response = await client.post("/v1/auth/register", json={"email": email})
    assert response.status_code == 201
    return response.json()


@pytest_asyncio.fixture
async def auth_headers(registered_user):
    return {"Authorization": f"Bearer {registered_user['api_key']}"}


@pytest_asyncio.fixture
async def other_auth_headers(client: AsyncClient):
    email = f"other-{uuid.uuid4().hex[:8]}@test.com"
    response = await client.post("/v1/auth/register", json={"email": email})
    assert response.status_code == 201
    return {"Authorization": f"Bearer {response.json()['api_key']}"}


@pytest_asyncio.fixture
async def redis_client():
    client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    yield client
    for pattern in ["ratelimit:*", "usage:*", "grace:*", "echo:*", "auth_dc:*", "auth_ml:*", "auth_ml_ip:*", "auth_poll:*", "echo_rl:*", "poller:*", "support_rl:*", "rotated:*", "failure_email:*"]:
        keys = await client.keys(pattern)
        if keys:
            await client.delete(*keys)
    if hasattr(client, "aclose"):
        await client.aclose()
    else:
        await client.close()


@pytest_asyncio.fixture
def db_engine():
    """Expose the test engine for poller/worker tests."""
    return engine
