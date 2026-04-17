"""Worker + evidence-based verification rejection.

This combo is rejected at cue create/update time because cueapi-worker
< 0.3.0 has no mechanism to attach evidence on the outcome report. The
rejection is lifted in a later PR once cueapi-worker 0.3.0 is on PyPI.

Eight tests: 3 evidence-requiring modes × (create, update, webhook
allowed) + 2 worker-compatible modes confirming the combo is allowed.
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


class TestWorkerEvidenceRejectedAtCreate:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "mode",
        ["require_external_id", "require_result_url", "require_artifacts"],
    )
    async def test_worker_plus_evidence_mode_rejected(
        self, client: AsyncClient, auth_headers, mode
    ):
        resp = await client.post(
            "/v1/cues",
            headers=auth_headers,
            json=_cue_body(transport="worker", mode=mode),
        )
        assert resp.status_code == 400
        body = resp.json()
        err = body["detail"]["error"] if "detail" in body else body["error"]
        assert err["code"] == "unsupported_verification_for_transport"
        assert err["transport"] == "worker"
        assert err["verification_mode"] == mode
        assert err["supported_worker_modes"] == ["none", "manual"]


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
    async def test_patch_worker_to_evidence_mode_rejected(
        self, client: AsyncClient, auth_headers
    ):
        # Create worker cue with no verification
        create = await client.post(
            "/v1/cues",
            headers=auth_headers,
            json=_cue_body(transport="worker"),
        )
        assert create.status_code == 201
        cue_id = create.json()["id"]

        # Try to PATCH verification to an evidence-requiring mode
        resp = await client.patch(
            f"/v1/cues/{cue_id}",
            headers=auth_headers,
            json={"verification": {"mode": "require_external_id"}},
        )
        assert resp.status_code == 400
        body = resp.json()
        err = body["detail"]["error"] if "detail" in body else body["error"]
        assert err["code"] == "unsupported_verification_for_transport"

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
        assert resp.json()["verification"] == {"mode": "manual"}
