from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from aiohttp import web
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.cue import Cue
from app.models.execution import Execution
from app.models.dispatch_outbox import DispatchOutbox
from app.utils.signing import verify_signature
from tests.test_poller import _create_due_cue, _create_test_user, _fresh_session


async def start_webhook_receiver(port: int):
    """Start a local HTTP server that records webhook callbacks."""
    received = []

    async def handler(request):
        body = await request.json()
        headers = dict(request.headers)
        received.append({"body": body, "headers": headers})
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/webhook", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "localhost", port)
    await site.start()
    return received, runner


async def _run_full_cycle(db_engine, cue_id=None):
    """Run poller → mark dispatched → direct task invocation."""
    from worker.poller import poll_due_cues
    from worker.tasks import deliver_webhook_task

    await poll_due_cues(db_engine, batch_size=500)

    session_factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    # Get outbox rows
    async with session_factory() as session:
        query = select(DispatchOutbox).where(DispatchOutbox.dispatched == False)  # noqa: E712
        if cue_id:
            query = query.where(DispatchOutbox.cue_id == cue_id)
        result = await session.execute(query.order_by(DispatchOutbox.created_at))
        outbox_rows = result.scalars().all()
        payloads = [(row.id, row.payload, row.task_type) for row in outbox_rows]

    # Mark outbox as dispatched
    for outbox_id, _, _ in payloads:
        async with db_engine.begin() as conn:
            await conn.execute(
                update(DispatchOutbox)
                .where(DispatchOutbox.id == outbox_id)
                .values(dispatched=True)
            )

    # Execute tasks directly
    ctx = {"db_session_factory": session_factory}
    for _, payload, task_type in payloads:
        if task_type == "deliver":
            await deliver_webhook_task(ctx, payload)
        else:
            from worker.tasks import retry_webhook_task
            await retry_webhook_task(ctx, payload)


@pytest.mark.asyncio
async def test_cue_fires_webhook(db_engine, db_session):
    """Create a cue due NOW, run poller+worker, verify callback received."""
    received, runner = await start_webhook_receiver(9999)
    try:
        user_id = await _create_test_user(db_session)
        cue = await _create_due_cue(
            db_session, user_id,
            name="fire-now",
            callback_url="http://localhost:9999/webhook",
            payload={"test": "hello"},
        )

        await _run_full_cycle(db_engine, cue_id=cue.id)

        assert len(received) == 1
        assert received[0]["body"]["cue_id"] == cue.id
        assert received[0]["body"]["payload"] == {"test": "hello"}
        headers_lower = {k.lower(): v for k, v in received[0]["headers"].items()}
        assert "x-cueapi-signature" in headers_lower
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_webhook_signature_is_valid(db_engine, db_session):
    """Verify the HMAC signature with per-user secret + timestamp."""
    from app.models.user import User

    received, runner = await start_webhook_receiver(9998)
    try:
        user_id = await _create_test_user(db_session)

        # Fetch the user's webhook_secret for verification
        result = await db_session.execute(
            select(User.webhook_secret).where(User.id == user_id)
        )
        user_secret = result.scalar_one()

        cue = await _create_due_cue(
            db_session, user_id,
            callback_url="http://localhost:9998/webhook",
        )

        await _run_full_cycle(db_engine, cue_id=cue.id)

        assert len(received) == 1
        body = received[0]["body"]
        headers_lower = {k.lower(): v for k, v in received[0]["headers"].items()}
        sig = headers_lower.get("x-cueapi-signature", "")
        timestamp = headers_lower.get("x-cueapi-timestamp", "")
        assert timestamp  # Must be present
        assert verify_signature(body, user_secret, timestamp, sig)
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_successful_delivery_marks_execution_success(db_engine, db_session):
    """Successful delivery should mark execution as 'success'."""
    received, runner = await start_webhook_receiver(9996)
    try:
        user_id = await _create_test_user(db_session)
        cue = await _create_due_cue(
            db_session, user_id,
            callback_url="http://localhost:9996/webhook",
        )

        await _run_full_cycle(db_engine, cue_id=cue.id)

        session = await _fresh_session(db_engine)
        try:
            result = await session.execute(
                select(Execution).where(Execution.cue_id == cue.id)
            )
            execution = result.scalar_one()
            assert execution.status == "success"
            assert execution.delivered_at is not None
            assert execution.http_status == 200
        finally:
            await session.close()
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_failed_webhook_creates_retry(db_engine, db_session):
    """Webhook to a non-existent URL should set execution to retrying."""
    user_id = await _create_test_user(db_session)
    cue = await _create_due_cue(
        db_session, user_id,
        callback_url="http://localhost:19999/nothing",
        retry_max_attempts=3,
        retry_backoff_minutes=[0, 0, 0],
    )

    await _run_full_cycle(db_engine, cue_id=cue.id)

    session = await _fresh_session(db_engine)
    try:
        result = await session.execute(
            select(Execution).where(Execution.cue_id == cue.id)
        )
        execution = result.scalar_one()
        assert execution.status == "retrying"
        assert execution.next_retry is not None
        assert execution.attempts == 1
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_all_retries_exhausted_marks_failed(db_engine, db_session):
    """After max retries, execution and one-time cue should be marked failed."""
    user_id = await _create_test_user(db_session)
    cue = await _create_due_cue(
        db_session, user_id,
        name="fail-fast",
        schedule_type="once",
        callback_url="http://localhost:19999/nothing",
        retry_max_attempts=1,
        retry_backoff_minutes=[0],
    )

    await _run_full_cycle(db_engine, cue_id=cue.id)

    session = await _fresh_session(db_engine)
    try:
        result = await session.execute(
            select(Execution).where(Execution.cue_id == cue.id)
        )
        execution = result.scalar_one()
        assert execution.status == "failed"
        assert execution.attempts == 1

        cue_result = await session.execute(
            select(Cue).where(Cue.id == cue.id)
        )
        updated_cue = cue_result.scalar_one()
        assert updated_cue.status == "failed"
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_recurring_cue_continues_after_failure(db_engine, db_session):
    """A recurring cue should stay active even after a failed execution."""
    user_id = await _create_test_user(db_session)
    cue = await _create_due_cue(
        db_session, user_id,
        name="keep-going",
        schedule_type="recurring",
        schedule_cron="* * * * *",
        callback_url="http://localhost:19999/nothing",
        retry_max_attempts=1,
        retry_backoff_minutes=[0],
    )

    await _run_full_cycle(db_engine, cue_id=cue.id)

    session = await _fresh_session(db_engine)
    try:
        cue_result = await session.execute(
            select(Cue).where(Cue.id == cue.id)
        )
        updated_cue = cue_result.scalar_one()
        assert updated_cue.status == "active"
        assert updated_cue.next_run is not None
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_onetime_cue_completed_on_success(db_engine, db_session):
    """One-time cue should be marked completed after successful delivery."""
    received, runner = await start_webhook_receiver(9997)
    try:
        user_id = await _create_test_user(db_session)
        cue = await _create_due_cue(
            db_session, user_id,
            name="one-and-done",
            schedule_type="once",
            callback_url="http://localhost:9997/webhook",
        )

        await _run_full_cycle(db_engine, cue_id=cue.id)

        session = await _fresh_session(db_engine)
        try:
            cue_result = await session.execute(
                select(Cue).where(Cue.id == cue.id)
            )
            updated_cue = cue_result.scalar_one()
            assert updated_cue.status == "completed"
        finally:
            await session.close()
    finally:
        await runner.cleanup()
