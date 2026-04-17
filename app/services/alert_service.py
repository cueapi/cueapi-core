"""Alert service — persist alerts and trigger delivery.

Dedup window: 5 minutes. Two alerts of the same
``(user_id, alert_type, execution_id)`` within that window collapse to
one row. Prevents alert storms on flapping executions.

Consecutive failures: when an outcome reports ``success=false``, count
the most recent N (default 3) completed executions on the same cue.
If all N failed, fire ``consecutive_failures``.

Webhook delivery: fire-and-forget. ``create_alert`` returns as soon as
the row is committed; ``deliver_alert`` is scheduled as a detached
task so slow/failing user webhooks never block the outcome-report
transaction. Any delivery exception is swallowed and logged inside
``deliver_alert`` itself — see ``alert_webhook.py``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alert import Alert
from app.models.execution import Execution
from app.models.user import User
from app.services.alert_webhook import deliver_alert

logger = logging.getLogger(__name__)

DEDUP_WINDOW_SECONDS = 300  # 5 minutes
CONSECUTIVE_FAILURE_THRESHOLD = 3


async def _recent_duplicate_exists(
    db: AsyncSession,
    user_id,
    alert_type: str,
    execution_id=None,
    cue_id: Optional[str] = None,
) -> bool:
    """Return True if a matching alert was already created inside the
    dedup window. Matching on (user_id, alert_type) plus whichever of
    (execution_id, cue_id) is non-null."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=DEDUP_WINDOW_SECONDS)
    stmt = (
        select(Alert.id)
        .where(
            Alert.user_id == user_id,
            Alert.alert_type == alert_type,
            Alert.created_at > cutoff,
        )
        .limit(1)
    )
    if execution_id is not None:
        stmt = stmt.where(Alert.execution_id == execution_id)
    elif cue_id is not None:
        stmt = stmt.where(Alert.cue_id == cue_id)
    existing = await db.scalar(stmt)
    return existing is not None


async def create_alert(
    db: AsyncSession,
    user_id,
    alert_type: str,
    message: str,
    severity: str = "warning",
    cue_id: Optional[str] = None,
    execution_id=None,
    metadata: Optional[dict] = None,
    schedule_delivery: bool = True,
) -> Optional[Alert]:
    """Persist an alert and schedule webhook delivery (if configured).

    Returns the created ``Alert`` or ``None`` if deduplicated. Writes
    in the caller's transaction (assumes the caller commits).

    ``schedule_delivery=False`` is a test hook — skips the detached
    delivery task so tests can assert on the DB row without racing a
    background coroutine.
    """
    if await _recent_duplicate_exists(
        db, user_id, alert_type, execution_id=execution_id, cue_id=cue_id
    ):
        logger.info(
            "Alert dedup: skipped within %ds window. type=%s user_id=%s execution_id=%s",
            DEDUP_WINDOW_SECONDS, alert_type, user_id, execution_id,
        )
        return None

    alert = Alert(
        id=uuid.uuid4(),
        user_id=user_id,
        cue_id=cue_id,
        execution_id=execution_id,
        alert_type=alert_type,
        severity=severity,
        message=message,
        alert_metadata=metadata,
    )
    db.add(alert)
    await db.flush()
    # Caller is expected to commit. We need ``created_at`` populated
    # for the webhook payload; ``flush`` populates server defaults
    # after commit only. Refresh below is done post-commit by caller
    # or by webhook's own retrieval. For fire-and-forget we capture
    # a snapshot now.
    await db.refresh(alert)

    if schedule_delivery:
        # Snapshot the URL + secret so the background task doesn't
        # need to reopen the session.
        user_row = await db.execute(
            select(User.alert_webhook_url, User.alert_webhook_secret)
            .where(User.id == user_id)
        )
        row = user_row.first()
        url = row.alert_webhook_url if row else None
        secret = row.alert_webhook_secret if row else None
        if url:
            # Fire-and-forget. The task is detached from the request
            # lifecycle on purpose — blocking here would couple outcome
            # latency to the user's webhook responsiveness.
            try:
                asyncio.create_task(deliver_alert(alert, url, secret))
            except RuntimeError:
                # No running event loop (shouldn't happen in the API
                # request path, but defensive for sync-context callers).
                logger.debug("No event loop to schedule alert delivery; skipping.")

    return alert


async def count_consecutive_failures(db: AsyncSession, cue_id: str) -> int:
    """Count the most recent run of consecutive failed executions on a
    cue. Walks the history backward and stops at the first non-failed
    row."""
    stmt = (
        select(Execution.status)
        .where(
            Execution.cue_id == cue_id,
            Execution.status.in_(["success", "failed"]),
        )
        .order_by(desc(Execution.created_at))
        .limit(CONSECUTIVE_FAILURE_THRESHOLD + 5)  # small over-read for safety
    )
    result = await db.execute(stmt)
    streak = 0
    for (status,) in result.all():
        if status == "failed":
            streak += 1
        else:
            break
    return streak


async def list_alerts(
    db: AsyncSession,
    user_id,
    alert_type: Optional[str] = None,
    since: Optional[datetime] = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    from sqlalchemy import func as sa_func

    query = select(Alert).where(Alert.user_id == user_id)
    count_query = select(sa_func.count(Alert.id)).where(Alert.user_id == user_id)

    if alert_type:
        query = query.where(Alert.alert_type == alert_type)
        count_query = count_query.where(Alert.alert_type == alert_type)
    if since is not None:
        query = query.where(Alert.created_at >= since)
        count_query = count_query.where(Alert.created_at >= since)

    total = await db.scalar(count_query) or 0
    rows = await db.execute(
        query.order_by(desc(Alert.created_at)).limit(limit).offset(offset)
    )
    return {
        "alerts": rows.scalars().all(),
        "total": total,
        "limit": limit,
        "offset": offset,
    }
