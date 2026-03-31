"""SDK integration tests.

Verify the cueapi-python SDK works correctly against the cueapi-core API.
Runs the ASGI app in a background thread and uses the SDK's sync HTTP client.
"""

from __future__ import annotations

import threading
import time
import uuid

import httpx
import pytest
import uvicorn

from app.main import app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TEST_PORT = 18927  # unlikely to collide


@pytest.fixture(scope="module", autouse=True)
def _start_server():
    """Start the ASGI server in a background thread for the test module."""
    config = uvicorn.Config(app, host="127.0.0.1", port=TEST_PORT, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for server to be ready
    for _ in range(40):
        try:
            r = httpx.get(f"http://127.0.0.1:{TEST_PORT}/status")
            if r.status_code == 200:
                break
        except httpx.ConnectError:
            pass
        time.sleep(0.25)
    else:
        pytest.fail("Server did not start in time")

    yield

    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def api_key():
    """Register a test user and return their API key."""
    email = f"sdk-test-{uuid.uuid4().hex[:8]}@test.com"
    r = httpx.post(
        f"http://127.0.0.1:{TEST_PORT}/v1/auth/register",
        json={"email": email},
    )
    assert r.status_code == 201, f"Registration failed: {r.text}"
    return r.json()["api_key"]


BASE = f"http://127.0.0.1:{TEST_PORT}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSDKCueLifecycle:
    """Test the full cue lifecycle through the SDK."""

    def test_create_cue(self, api_key: str):
        """SDK can create a recurring cue."""
        from cueapi import CueAPI

        with CueAPI(api_key, base_url=BASE) as client:
            cue = client.cues.create(
                name=f"sdk-test-{uuid.uuid4().hex[:6]}",
                cron="0 9 * * *",
                transport="worker",
            )
            assert cue.id is not None
            assert cue.id.startswith("cue_")
            assert cue.status == "active"

            client.cues.delete(cue.id)

    def test_list_cues(self, api_key: str):
        """SDK can list cues and find a created cue."""
        from cueapi import CueAPI

        with CueAPI(api_key, base_url=BASE) as client:
            name = f"sdk-list-{uuid.uuid4().hex[:6]}"
            cue = client.cues.create(name=name, cron="0 12 * * *", transport="worker")

            cue_list = client.cues.list()
            assert cue_list.total >= 1
            assert any(c.id == cue.id for c in cue_list.cues)

            client.cues.delete(cue.id)

    def test_get_cue(self, api_key: str):
        """SDK can get a specific cue by ID."""
        from cueapi import CueAPI

        with CueAPI(api_key, base_url=BASE) as client:
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

    def test_update_cue(self, api_key: str):
        """SDK can update a cue."""
        from cueapi import CueAPI

        with CueAPI(api_key, base_url=BASE) as client:
            cue = client.cues.create(
                name=f"sdk-update-{uuid.uuid4().hex[:6]}",
                cron="0 6 * * *",
                transport="worker",
            )
            updated = client.cues.update(cue.id, description="Updated by SDK test", cron="0 7 * * *")
            assert updated.description == "Updated by SDK test"

            client.cues.delete(cue.id)

    def test_pause_resume_cue(self, api_key: str):
        """SDK can pause and resume a cue."""
        from cueapi import CueAPI

        with CueAPI(api_key, base_url=BASE) as client:
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

    def test_delete_cue(self, api_key: str):
        """SDK can delete a cue and verify it is gone."""
        from cueapi import CueAPI
        from cueapi.exceptions import CueNotFoundError

        with CueAPI(api_key, base_url=BASE) as client:
            cue = client.cues.create(
                name=f"sdk-delete-{uuid.uuid4().hex[:6]}",
                cron="0 4 * * *",
                transport="worker",
            )
            cue_id = cue.id
            client.cues.delete(cue_id)

            with pytest.raises(CueNotFoundError):
                client.cues.get(cue_id)

    def test_create_one_time_cue(self, api_key: str):
        """SDK can create a one-time cue."""
        from cueapi import CueAPI

        with CueAPI(api_key, base_url=BASE) as client:
            cue = client.cues.create(
                name=f"sdk-once-{uuid.uuid4().hex[:6]}",
                at="2099-01-01T00:00:00Z",
                transport="worker",
            )
            assert cue.id is not None
            assert cue.status == "active"

            client.cues.delete(cue.id)

    def test_auth_error_on_bad_key(self):
        """SDK raises AuthenticationError with an invalid API key."""
        from cueapi import CueAPI
        from cueapi.exceptions import AuthenticationError

        with CueAPI("cue_sk_invalid_key_0000000000000000", base_url=BASE) as client:
            with pytest.raises(AuthenticationError):
                client.cues.list()
