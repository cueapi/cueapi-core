"""SDK integration tests.

Verify the cueapi-python SDK works correctly against the cueapi-core API.
These tests run against the ASGI test server with a real Postgres database.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest_asyncio.fixture
async def api_key():
    """Register a user and return their API key."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        email = f"sdk-test-{uuid.uuid4().hex[:8]}@test.com"
        resp = await ac.post("/v1/auth/register", json={"email": email})
        assert resp.status_code == 201
        data = resp.json()
        return data["api_key"]


class TestSDKCueLifecycle:
    """Test the full cue lifecycle through the SDK."""

    @pytest.mark.asyncio
    async def test_create_cue(self, api_key: str):
        """SDK can create a recurring cue."""
        from cueapi import CueAPI

        with CueAPI(api_key, base_url="http://test") as client:
            # Patch the httpx client to use ASGI transport
            client._http = _make_test_http(api_key)

            cue = client.cues.create(
                name=f"sdk-test-{uuid.uuid4().hex[:6]}",
                cron="0 9 * * *",
                transport="worker",
            )

            assert cue.id is not None
            assert cue.id.startswith("cue_")
            assert cue.status == "active"
            assert cue.name.startswith("sdk-test-")

            # Clean up
            client.cues.delete(cue.id)

    @pytest.mark.asyncio
    async def test_list_cues(self, api_key: str):
        """SDK can list cues and find a created cue."""
        from cueapi import CueAPI

        with CueAPI(api_key, base_url="http://test") as client:
            client._http = _make_test_http(api_key)

            name = f"sdk-list-{uuid.uuid4().hex[:6]}"
            cue = client.cues.create(
                name=name,
                cron="0 12 * * *",
                transport="worker",
            )

            cue_list = client.cues.list()
            assert cue_list.total >= 1

            found = any(c.id == cue.id for c in cue_list.cues)
            assert found, f"Created cue {cue.id} not found in list"

            client.cues.delete(cue.id)

    @pytest.mark.asyncio
    async def test_get_cue(self, api_key: str):
        """SDK can get a specific cue by ID."""
        from cueapi import CueAPI

        with CueAPI(api_key, base_url="http://test") as client:
            client._http = _make_test_http(api_key)

            cue = client.cues.create(
                name=f"sdk-get-{uuid.uuid4().hex[:6]}",
                cron="30 8 * * 1-5",
                transport="worker",
                payload={"task": "test"},
            )

            fetched = client.cues.get(cue.id)
            assert fetched.id == cue.id
            assert fetched.name == cue.name

            client.cues.delete(cue.id)

    @pytest.mark.asyncio
    async def test_update_cue(self, api_key: str):
        """SDK can update a cue's schedule and description."""
        from cueapi import CueAPI

        with CueAPI(api_key, base_url="http://test") as client:
            client._http = _make_test_http(api_key)

            cue = client.cues.create(
                name=f"sdk-update-{uuid.uuid4().hex[:6]}",
                cron="0 6 * * *",
                transport="worker",
            )

            updated = client.cues.update(
                cue.id,
                description="Updated by SDK test",
                cron="0 7 * * *",
            )
            assert updated.description == "Updated by SDK test"

            client.cues.delete(cue.id)

    @pytest.mark.asyncio
    async def test_pause_resume_cue(self, api_key: str):
        """SDK can pause and resume a cue."""
        from cueapi import CueAPI

        with CueAPI(api_key, base_url="http://test") as client:
            client._http = _make_test_http(api_key)

            cue = client.cues.create(
                name=f"sdk-pause-{uuid.uuid4().hex[:6]}",
                cron="0 3 * * *",
                transport="worker",
            )

            paused = client.cues.pause(cue.id)
            assert paused.status == "paused"

            resumed = client.cues.resume(cue.id)
            assert resumed.status == "active"

            client.cues.delete(cue.id)

    @pytest.mark.asyncio
    async def test_delete_cue(self, api_key: str):
        """SDK can delete a cue and verify it is gone."""
        from cueapi import CueAPI
        from cueapi.exceptions import CueNotFoundError

        with CueAPI(api_key, base_url="http://test") as client:
            client._http = _make_test_http(api_key)

            cue = client.cues.create(
                name=f"sdk-delete-{uuid.uuid4().hex[:6]}",
                cron="0 4 * * *",
                transport="worker",
            )
            cue_id = cue.id

            client.cues.delete(cue_id)

            with pytest.raises(CueNotFoundError):
                client.cues.get(cue_id)

    @pytest.mark.asyncio
    async def test_create_one_time_cue(self, api_key: str):
        """SDK can create a one-time cue with 'at' parameter."""
        from cueapi import CueAPI

        with CueAPI(api_key, base_url="http://test") as client:
            client._http = _make_test_http(api_key)

            cue = client.cues.create(
                name=f"sdk-once-{uuid.uuid4().hex[:6]}",
                at="2099-01-01T00:00:00Z",
                transport="worker",
            )

            assert cue.id is not None
            assert cue.status == "active"

            client.cues.delete(cue.id)

    @pytest.mark.asyncio
    async def test_auth_error_on_bad_key(self):
        """SDK raises AuthenticationError with an invalid API key."""
        from cueapi import CueAPI
        from cueapi.exceptions import AuthenticationError

        with CueAPI("cue_sk_invalid_key_that_does_not_exist", base_url="http://test") as client:
            client._http = _make_test_http("cue_sk_invalid_key_that_does_not_exist")

            with pytest.raises(AuthenticationError):
                client.cues.list()


class _ClosableASGITransport(httpx.ASGITransport):
    """ASGITransport with a no-op close() for sync httpx.Client compatibility."""

    def close(self) -> None:
        pass


def _make_test_http(api_key: str):
    """Create an httpx.Client using the ASGI transport for local testing."""
    return httpx.Client(
        transport=_ClosableASGITransport(app=app),
        base_url="http://test",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "cueapi-python/0.1.0",
        },
        timeout=30.0,
    )
