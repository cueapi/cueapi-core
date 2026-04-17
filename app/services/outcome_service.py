from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthenticatedUser
from app.models.cue import Cue
from app.models.execution import Execution
from app.schemas.outcome import OutcomeRequest, OutcomeResponse

logger = logging.getLogger(__name__)


async def record_outcome(
    db: AsyncSession, user: AuthenticatedUser, execution_id: str, body: OutcomeRequest
) -> dict:
    """Record the outcome of an execution. Write-once — returns 409 if already set.

    For worker-transport cues, this also completes the execution lifecycle:
    sets execution status to success/failed, updates cue run_count/last_run,
    and marks one-time cues as completed/failed.
    """

    # Find execution and verify ownership via cue -> user_id
    # Also fetch cue transport and schedule_type for worker lifecycle
    # Use FOR UPDATE to prevent concurrent outcome submissions (write-once)
    result = await db.execute(
        select(Execution, Cue.callback_transport, Cue.schedule_type)
        .join(Cue, Execution.cue_id == Cue.id)
        .where(Execution.id == execution_id, Cue.user_id == user.id)
        .with_for_update(of=Execution)
    )
    row = result.one_or_none()

    if row is None:
        return {
            "error": {
                "code": "execution_not_found",
                "message": "Execution not found",
                "status": 404,
            }
        }

    execution = row[0]
    transport = row[1]
    schedule_type = row[2]

    # Write-once check (row is locked by FOR UPDATE, so this is race-safe)
    if execution.outcome_recorded_at is not None:
        return {
            "error": {
                "code": "outcome_already_recorded",
                "message": "Outcome has already been recorded for this execution",
                "status": 409,
            }
        }

    # Validate metadata size (<=10KB JSON)
    if body.metadata is not None:
        metadata_size = len(json.dumps(body.metadata).encode("utf-8"))
        if metadata_size > 10_240:
            return {
                "error": {
                    "code": "metadata_too_large",
                    "message": "Outcome metadata must be under 10KB",
                    "status": 400,
                }
            }

    now = datetime.now(timezone.utc)

    # Write outcome fields
    execution.outcome_success = body.success
    execution.outcome_result = body.result
    execution.outcome_error = body.error
    execution.outcome_metadata = body.metadata
    execution.outcome_recorded_at = now

    # For worker transport, the outcome IS the completion signal.
    # Update execution status and cue lifecycle (mirroring _handle_success/_handle_failure).
    if transport == "worker":
        # Increment run_count on every outcome (not just success)
        await db.execute(
            update(Cue)
            .where(Cue.id == execution.cue_id)
            .values(
                last_run=now,
                run_count=Cue.run_count + 1,
                updated_at=now,
            )
        )

        if body.success:
            execution.status = "success"
            execution.delivered_at = now
            execution.updated_at = now

            # One-time cue → mark completed
            if schedule_type == "once":
                await db.execute(
                    update(Cue)
                    .where(Cue.id == execution.cue_id)
                    .values(status="completed", updated_at=now)
                )
        else:
            execution.status = "failed"
            execution.error_message = body.error
            execution.updated_at = now

            # One-time cue → mark failed
            if schedule_type == "once":
                await db.execute(
                    update(Cue)
                    .where(Cue.id == execution.cue_id)
                    .values(status="failed", updated_at=now)
                )

    await db.commit()

    # ── Alert firing (best-effort, post-commit) ──
    # Each branch uses create_alert's dedup window (5 min) to collapse
    # storms. Webhook delivery is fire-and-forget inside create_alert.
    try:
        from app.services.alert_service import (
            CONSECUTIVE_FAILURE_THRESHOLD,
            count_consecutive_failures,
            create_alert,
        )

        # verification_failed: set by the PR #18 verification rule
        # engine when required evidence is missing. On current main
        # (pre-#18), nothing sets outcome_state to 'verification_failed'
        # during record_outcome, so this branch is dormant. Once PR #18
        # merges, the hook fires automatically without further changes.
        if getattr(execution, "outcome_state", None) == "verification_failed":
            await create_alert(
                db,
                user_id=user.id,
                alert_type="verification_failed",
                severity="warning",
                message=(
                    f"Execution {execution_id} reported success but failed "
                    f"verification (required evidence missing)."
                ),
                execution_id=execution_id,
                cue_id=execution.cue_id,
                metadata={
                    "outcome_state": "verification_failed",
                    "transport": transport,
                },
            )
            await db.commit()

        # consecutive_failures: on a failed outcome, walk recent
        # executions on this cue. If threshold reached, fire once
        # (dedup keeps subsequent failures quiet for 5 min).
        if not body.success:
            streak = await count_consecutive_failures(db, execution.cue_id)
            if streak >= CONSECUTIVE_FAILURE_THRESHOLD:
                await create_alert(
                    db,
                    user_id=user.id,
                    alert_type="consecutive_failures",
                    severity="warning",
                    message=(
                        f"Cue {execution.cue_id} has {streak} consecutive "
                        f"failed executions."
                    ),
                    execution_id=execution_id,
                    cue_id=execution.cue_id,
                    metadata={"consecutive_failures": streak},
                )
                await db.commit()
    except Exception:
        # Alert firing must never break outcome reporting.
        logger.exception(
            "Alert firing failed for execution %s (outcome was still recorded)",
            execution_id,
        )

    logger.info(
        "Outcome recorded",
        extra={
            "event_type": "outcome_recorded",
            "execution_id": execution_id,
            "success": body.success,
            "transport": transport,
        },
    )

    return {
        "outcome": OutcomeResponse(
            execution_id=execution_id,
            outcome_recorded=True,
        )
    }
