import pytest


@pytest.mark.asyncio
async def test_health_returns_ok(client):
    response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] in ("healthy", "degraded")
    assert data["services"]["postgres"] == "ok"
    assert data["services"]["redis"] == "ok"


@pytest.mark.asyncio
async def test_health_returns_request_id(client):
    response = await client.get("/health")
    assert "x-request-id" in response.headers
