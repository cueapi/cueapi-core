"""Tests for on_failure escalation config: email, webhook, pause.

Also tests meaningful error messages and run_count-on-attempt behavior.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from aiohttp import web
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.cue import Cue
from app.models.execution import Execution
from app.models.dispatch_outbox import DispatchOutbox
from app.services.webhook import deliver_webhook, _meaningful_error
from tests.test_poller import _create_due_cue, _create_test_user, _fresh_session


# ── Helper: run full cycle with failure (unreachable URL) ───────

async def _run_full_cycle_fail(db_engine, cue_id=None):
    """Run poller → mark dispatched → direct task invocation (expects failure)."""
    from worker.poller import poll_due_cues
    from worker.tasks import deliver_webhook_task

    await poll_due_cues(db_engine, batch_size=500)

    session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as session:
        query = select(DispatchOutbox).where(DispatchOutbox.dispatched == False)  # noqa: E712
        if cue_id:
            query = query.where(DispatchOutbox.cue_id == cue_id)
        result = await session.execute(query.order_by(DispatchOutbox.created_at))
        outbox_rows = result.scalars().all()
        payloads = [(row.id, row.payload, row.task_type) for row in outbox_rows]

    for outbox_id, _, _ in payloads:
        async with db_engine.begin() as conn:
            await conn.execute(
                update(DispatchOutbox)
                .where(DispatchOutbox.id == outbox_id)
                .values(dispatched=True)
            )

    ctx = {"db_session_factory": session_factory}
    for _, payload, task_type in payloads:
        if task_type == "deliver":
            await deliver_webhook_task(ctx, payload)
        else:
            from worker.tasks import retry_webhook_task
            await retry_webhook_task(ctx, payload)


# ── on_failure.email tests ──────────────────────────────────────

@pytest.mark.asyncio
async def test_on_failure_email_sent_on_final_failure(db_engine, db_session):
    """When on_failure.email=true and retries exhausted, email is sent."""
    user_id = await _create_test_user(db_session)
    cue = await _create_due_cue(
        db_session, user_id,
        name="fail-email",
        schedule_type="once",
        callback_url="http://localhost:19999/nothing",
        retry_max_attempts=1,
        retry_backoff_minutes=[0],
    )
    # Set on_failure config
    await db_session.execute(
        update(Cue).where(Cue.id == cue.id).values(
            on_failure={"email": True, "webhook": None, "pause": False}
        )
    )
    await db_session.commit()

    with patch("worker.tasks._send_failure_email", new_callable=AsyncMock) as mock_email:
        await _run_full_cycle_fail(db_engine, cue_id=cue.id)
        mock_email.assert_called_once()
        call_args = mock_email.call_args
        assert call_args[0][2] == cue.id  # cue_id arg


@pytest.mark.asyncio
async def test_on_failure_defaults_to_email_true(db_engine, db_session):
    """When on_failure is NULL/default, email should still be sent (default: email=true)."""
    user_id = await _create_test_user(db_session)
    cue = await _create_due_cue(
        db_session, user_id,
        name="fail-default",
        schedule_type="once",
        callback_url="http://localhost:19999/nothing",
        retry_max_attempts=1,
        retry_backoff_minutes=[0],
    )
    # Leave on_failure as NULL (default behavior)

    with patch("worker.tasks._send_failure_email", new_callable=AsyncMock) as mock_email:
        await _run_full_cycle_fail(db_engine, cue_id=cue.id)
        mock_email.assert_called_once()


# ── on_failure.webhook tests ────────────────────────────────────

@pytest.mark.asyncio
async def test_on_failure_webhook_called_on_final_failure(db_engine, db_session):
    """When on_failure.webhook is set, POST failure details to that URL."""
    received = []

    async def handler(request):
        body = await request.json()
        received.append(body)
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/alerts", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "localhost", 19877)
    await site.start()

    try:
        user_id = await _create_test_user(db_session)
        cue = await _create_due_cue(
            db_session, user_id,
            name="fail-webhook",
            schedule_type="once",
            callback_url="http://localhost:19999/nothing",
            retry_max_attempts=1,
            retry_backoff_minutes=[0],
        )
        await db_session.execute(
            update(Cue).where(Cue.id == cue.id).values(
                on_failure={"email": False, "webhook": "http://localhost:19877/alerts", "pause": False}
            )
        )
        await db_session.commit()

        with patch("worker.tasks._send_failure_email", new_callable=AsyncMock):
            await _run_full_cycle_fail(db_engine, cue_id=cue.id)

        assert len(received) == 1
        assert received[0]["event"] == "cue.failed"
        assert received[0]["cue_id"] == cue.id
        assert received[0]["cue_name"] == "fail-webhook"
        assert "attempts" in received[0]
        assert "failed_at" in received[0]
        assert received[0]["dashboard_url"] == "https://dashboard.cueapi.ai"
    finally:
        await runner.cleanup()


# ── on_failure.pause tests ──────────────────────────────────────

@pytest.mark.asyncio
async def test_on_failure_pause_on_final_failure(db_engine, db_session):
    """When on_failure.pause=true, recurring cue is paused after final failure."""
    user_id = await _create_test_user(db_session)
    cue = await _create_due_cue(
        db_session, user_id,
        name="fail-pause",
        schedule_type="recurring",
        schedule_cron="* * * * *",
        callback_url="http://localhost:19999/nothing",
        retry_max_attempts=1,
        retry_backoff_minutes=[0],
    )
    await db_session.execute(
        update(Cue).where(Cue.id == cue.id).values(
            on_failure={"email": False, "webhook": None, "pause": True}
        )
    )
    await db_session.commit()

    with patch("worker.tasks._send_failure_email", new_callable=AsyncMock):
        await _run_full_cycle_fail(db_engine, cue_id=cue.id)

    session = await _fresh_session(db_engine)
    try:
        result = await session.execute(select(Cue).where(Cue.id == cue.id))
        updated_cue = result.scalar_one()
        assert updated_cue.status == "paused"
        assert updated_cue.next_run is None
    finally:
        await session.close()


# ── Meaningful error message tests ──────────────────────────────

def test_meaningful_error_message_404():
    assert _meaningful_error(404) == "Webhook endpoint not found (404)"


def test_meaningful_error_message_500():
    assert _meaningful_error(500) == "Webhook endpoint returned server error (500)"


@pytest.mark.asyncio
async def test_meaningful_error_message_timeout():
    """Timeout should return specific error message."""
    with patch("app.services.webhook.validate_url_at_delivery", return_value=(True, "")):
        with patch("httpx.AsyncClient") as MockClient:
            import httpx
            mock_instance = AsyncMock()
            mock_instance.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_instance

            success, status, body = await deliver_webhook(
                callback_url="https://example.com/hook",
                callback_method="POST",
                callback_headers={},
                payload={},
                cue_id="cue-timeout",
                cue_name="timeout-test",
                execution_id="exec-timeout",
                scheduled_for=datetime.now(timezone.utc),
                attempt=1,
            )
            assert success is False
            assert body == "Webhook endpoint timed out"


# ── run_count on attempt tests ──────────────────────────────────

@pytest.mark.asyncio
async def test_run_count_increments_on_attempt(db_engine, db_session):
    """run_count should increment even when delivery fails (not just on success)."""
    user_id = await _create_test_user(db_session)
    cue = await _create_due_cue(
        db_session, user_id,
        name="run-count-test",
        schedule_type="once",
        callback_url="http://localhost:19999/nothing",
        retry_max_attempts=1,
        retry_backoff_minutes=[0],
    )

    # Verify initial run_count is 0
    session = await _fresh_session(db_engine)
    try:
        result = await session.execute(select(Cue.run_count).where(Cue.id == cue.id))
        initial_count = result.scalar_one()
        assert initial_count == 0
    finally:
        await session.close()

    with patch("worker.tasks._send_failure_email", new_callable=AsyncMock):
        await _run_full_cycle_fail(db_engine, cue_id=cue.id)

    # After failed delivery, run_count should be 1 (incremented on attempt)
    session = await _fresh_session(db_engine)
    try:
        result = await session.execute(select(Cue.run_count).where(Cue.id == cue.id))
        final_count = result.scalar_one()
        assert final_count == 1, f"Expected run_count=1 after failed attempt, got {final_count}"
    finally:
        await session.close()
