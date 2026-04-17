"""Worker + evidence-based verification: now accepted.

History: this file previously pinned the rejection of
``(worker, require_*)`` combos because cueapi-worker < 0.3.0 had no
mechanism to attach evidence on the outcome report. Those tests
asserted 400 on create and PATCH.

cueapi-worker 0.3.0 (CUEAPI_OUTCOME_FILE) shipped to PyPI 2026-04-17
and closes that gap: the handler writes evidence to a per-run temp
file, the daemon merges it into the outcome POST. The rejection was
lifted in the PR that replaces this file's content.

The assertions below now pin the accept behavior. Retained for
regression: if anyone reintroduces the combo-rejection they'll see
these tests fail and have to rationalize the rollback.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient


def _cue_body(*, transport="worker", mode=None, name=None):
    body = {
        "name": name or f"combo-{uuid.uuid4().hex[:6]}",
        "schedule": {
            "type": "once",
            "at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "timezone": "UTC",
        },
        "transport": transport,
        "payload": {"task": "t"},
    }
    if transport == "webhook":
        body["callback"] = {"url": "https://example.com/hook"}
    if mode is not None:
        body["verification"] = {"mode": mode}
    return body


class TestWorkerEvidenceAcceptedAtCreate:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "mode",
        ["require_external_id", "require_result_url", "require_artifacts"],
    )
    async def test_worker_plus_evidence_mode_accepted(
        self, client: AsyncClient, auth_headers, mode
    ):
        resp = await client.post(
            "/v1/cues",
            headers=auth_headers,
            json=_cue_body(transport="worker", mode=mode),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["verification"] == {"mode": mode}
        assert body["transport"] == "worker" or body["callback"]["transport"] == "worker"


class TestWorkerCompatibleModesAcceptedAtCreate:
    @pytest.mark.asyncio
    async def test_worker_none_accepted(self, client: AsyncClient, auth_headers):
        resp = await client.post(
            "/v1/cues",
            headers=auth_headers,
            json=_cue_body(transport="worker", mode="none"),
        )
        assert resp.status_code == 201, resp.text

    @pytest.mark.asyncio
    async def test_worker_manual_accepted(self, client: AsyncClient, auth_headers):
        resp = await client.post(
            "/v1/cues",
            headers=auth_headers,
            json=_cue_body(transport="worker", mode="manual"),
        )
        assert resp.status_code == 201, resp.text


class TestWebhookAllModesAccepted:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "mode",
        [
            "none",
            "require_external_id",
            "require_result_url",
            "require_artifacts",
            "manual",
        ],
    )
    async def test_webhook_any_mode_accepted(
        self, client: AsyncClient, auth_headers, mode
    ):
        resp = await client.post(
            "/v1/cues",
            headers=auth_headers,
            json=_cue_body(transport="webhook", mode=mode),
        )
        assert resp.status_code == 201, resp.text


class TestPatchTransitions:
    @pytest.mark.asyncio
    async def test_patch_worker_to_evidence_mode_accepted(
        self, client: AsyncClient, auth_headers
    ):
        # Create worker cue with no verification, then PATCH to an
        # evidence-requiring mode. Previously this returned 400; now 200.
        create = await client.post(
            "/v1/cues",
            headers=auth_headers,
            json=_cue_body(transport="worker"),
        )
        assert create.status_code == 201
        cue_id = create.json()["id"]

        resp = await client.patch(
            f"/v1/cues/{cue_id}",
            headers=auth_headers,
            json={"verification": {"mode": "require_external_id"}},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["verification"] == {"mode": "require_external_id"}

    @pytest.mark.asyncio
    async def test_patch_webhook_to_evidence_mode_accepted(
        self, client: AsyncClient, auth_headers
    ):
        create = await client.post(
            "/v1/cues",
            headers=auth_headers,
            json=_cue_body(transport="webhook"),
        )
        assert create.status_code == 201
        cue_id = create.json()["id"]

        resp = await client.patch(
            f"/v1/cues/{cue_id}",
            headers=auth_headers,
            json={"verification": {"mode": "require_result_url"}},
        )
        assert resp.status_code == 200
        assert resp.json()["verification"] == {"mode": "require_result_url"}

    @pytest.mark.asyncio
    async def test_patch_worker_to_manual_accepted(
        self, client: AsyncClient, auth_headers
    ):
        create = await client.post(
            "/v1/cues",
            headers=auth_headers,
            json=_cue_body(transport="worker"),
        )
        assert create.status_code == 201
        cue_id = create.json()["id"]

        resp = await client.patch(
            f"/v1/cues/{cue_id}",
            headers=auth_headers,
            json={"verification": {"mode": "manual"}},
        )
        assert resp.status_code == 200
