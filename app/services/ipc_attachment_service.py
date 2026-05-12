"""Item B Phase 1 — service layer for IPC attachment lifecycle.

Pure-helper functions backing the three IPC attachment endpoints. Extracted
from the router layer so pytest-cov can trace branches without going through
the ASGI dispatch wrapper (per CLAUDE.md "Pure-helper extraction" discipline,
established for the verify_echo + cursor-advance-as-ack work).

Design contract (from joint design lock + Mike Q-B ratify 2026-05-12 ~00:38Z):

* Attachments are scoped per-(agent_id, label, daemon_id). Same-label
  re-attach from same daemon REPLACES (issues new token, supersedes old).
* Cross-daemon collision on same (agent_id, label) returns 409 with the
  existing row's daemon_id so daemon can escalate.
* DELETE is idempotent. First-time = 204; subsequent = 200 with reason.
* Reconcile is one atomic UPSERT transaction. Daemon-scoped: rows belonging
  to other daemons untouched.
* Token revocation paths: explicit DELETE / reattach supersede / daemon-
  absence cleanup (>24h). All log structured ``attachment_token_revoked``.
* Fire-accept dispatcher uses ASYNC path (Mike Q-B): server fires, returns
  immediately with ``delivery_mode_requested='ipc'``; daemon ACKs via the
  existing ``POST /v1/executions/<id>/outcome`` path. NO inline sync ack.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.agent_live_session import AgentLiveSession
from app.schemas.ipc_attachment import (
    AttachmentReconcileEntry,
)


logger = logging.getLogger(__name__)


# ───────────────────────────────────────────────────────────────────────
# Outcomes — discriminated-union results for each service-layer call so
# the router stays a thin wrapper that maps to HTTP shapes.
# ───────────────────────────────────────────────────────────────────────


class AttachmentCreateResult:
    """Result of ``create_attachment``. One of: created, conflict_same_daemon
    (supersede), conflict_cross_daemon (409)."""

    __slots__ = ("status", "row", "existing", "supersedes_token")

    def __init__(
        self,
        *,
        status: str,
        row: Optional[AgentLiveSession] = None,
        existing: Optional[AgentLiveSession] = None,
        supersedes_token: Optional[str] = None,
    ):
        # status ∈ {"created", "conflict_cross_daemon"}
        # supersede-same-daemon is handled inline by replacing the row; result
        # carries status="created" with supersedes_token set.
        self.status = status
        self.row = row
        self.existing = existing
        self.supersedes_token = supersedes_token


class AttachmentDeleteResult:
    """Result of ``delete_attachment``. One of: deleted, already_deleted."""

    __slots__ = ("status",)

    def __init__(self, *, status: str):
        # status ∈ {"deleted", "already_deleted"}
        self.status = status


class AttachmentReconcileResult:
    """Result of ``reconcile_attachments``."""

    __slots__ = ("upserted_count", "downgraded_count")

    def __init__(self, *, upserted_count: int, downgraded_count: int):
        self.upserted_count = upserted_count
        self.downgraded_count = downgraded_count


# ───────────────────────────────────────────────────────────────────────
# Resolution + scoping helpers
# ───────────────────────────────────────────────────────────────────────


async def _resolve_agent(db: AsyncSession, agent_ref: str) -> Optional[Agent]:
    """Find an Agent by opaque ID (`agt_xxx`) or slug-form (`slug@owner`).

    Mirrors ``app.services.agent_service.resolve_address`` but kept local
    so we can return ``None`` instead of raising — the router maps to 404.
    """
    if agent_ref.startswith("agt_"):
        row = (await db.execute(select(Agent).where(Agent.id == agent_ref))).scalar_one_or_none()
        return row
    # Slug-form: `slug@user_slug`. Phase 1 keeps this resolution minimal;
    # multi-tenant slug resolution lives in agent_service.resolve_address.
    return None


# ───────────────────────────────────────────────────────────────────────
# create_attachment — POST /v1/agents/<ref>/attachments
# ───────────────────────────────────────────────────────────────────────


async def create_attachment(
    db: AsyncSession,
    *,
    agent_id: str,
    label: str,
    task_name: str,
    ipc_session_token: str,
    daemon_id: UUID,
    attached_at: Optional[datetime] = None,
    monitor_version: Optional[str] = None,
) -> AttachmentCreateResult:
    """Create or supersede an IPC attachment for ``(agent_id, label, daemon_id)``.

    Behavior matrix:

    * No active row with same ``(agent_id, label)`` → INSERT new row, return
      ``status='created'``.
    * Active row exists with same ``(agent_id, label, daemon_id)`` → SUPERSEDE
      (mark old token revoked, INSERT new). Returns ``status='created'`` with
      ``supersedes_token`` set so the daemon knows their prior session was
      replaced (informational; old token is invalid immediately).
    * Active row exists with same ``(agent_id, label)`` but DIFFERENT
      ``daemon_id`` → return ``status='conflict_cross_daemon'`` with
      ``existing`` populated. Router maps to 409 ``attachment_exists``.
    """
    now = attached_at or datetime.now(timezone.utc)

    # Find any active row for this (agent_id, label) — supersede vs conflict
    # is determined by daemon_id comparison.
    existing_query = select(AgentLiveSession).where(
        and_(
            AgentLiveSession.agent_id == agent_id,
            AgentLiveSession.label == label,
            AgentLiveSession.detached_at.is_(None),
        )
    )
    existing = (await db.execute(existing_query)).scalar_one_or_none()

    if existing is not None:
        if existing.daemon_id == daemon_id:
            # Same daemon → supersede. Mark old row detached + insert new.
            existing.detached_at = now
            old_token = existing.ipc_session_token
            logger.info(
                "attachment_token_revoked",
                extra={
                    "agent_id": agent_id,
                    "label": label,
                    "daemon_id": str(daemon_id),
                    "old_token": old_token,
                    "new_token": ipc_session_token,
                    "reason": "reattach_supersede",
                },
            )
            # cue_id space note: the table's ix_agent_live_sessions_cue_id
            # is globally unique INCLUDING soft-detached rows. Reusing the
            # old row's cue_id on the new row would collide with the
            # detached row. Use the new ipc_session_token as cue_id —
            # daemon-issued ULIDs are globally unique by construction.
            new_row = AgentLiveSession(
                agent_id=agent_id,
                label=label,
                task_name=task_name,
                cue_id=ipc_session_token,
                is_default=existing.is_default,
                attached_at=now,
                ipc_session_token=ipc_session_token,
                transport="ipc",
                daemon_id=daemon_id,
                last_reconciled_at=now,
                monitor_version=monitor_version,
            )
            db.add(new_row)
            await db.flush()
            return AttachmentCreateResult(
                status="created", row=new_row, supersedes_token=old_token
            )
        # Cross-daemon → caller should escalate; do NOT clobber.
        return AttachmentCreateResult(status="conflict_cross_daemon", existing=existing)

    # No existing row → simple INSERT.
    new_row = AgentLiveSession(
        agent_id=agent_id,
        label=label,
        task_name=task_name,
        cue_id=ipc_session_token,  # Phase 1: use token as cue_id stand-in
        is_default=(label == "main"),
        attached_at=now,
        ipc_session_token=ipc_session_token,
        transport="ipc",
        daemon_id=daemon_id,
        last_reconciled_at=now,
        monitor_version=monitor_version,
    )
    db.add(new_row)
    await db.flush()
    return AttachmentCreateResult(status="created", row=new_row)


# ───────────────────────────────────────────────────────────────────────
# delete_attachment — DELETE /v1/agents/<ref>/attachments/<token>
# ───────────────────────────────────────────────────────────────────────


async def delete_attachment(
    db: AsyncSession,
    *,
    agent_id: str,
    ipc_session_token: str,
    daemon_id: UUID,
) -> AttachmentDeleteResult:
    """Idempotent DELETE by token. Scoped to caller's daemon_id."""
    query = select(AgentLiveSession).where(
        and_(
            AgentLiveSession.agent_id == agent_id,
            AgentLiveSession.ipc_session_token == ipc_session_token,
            AgentLiveSession.daemon_id == daemon_id,
            AgentLiveSession.detached_at.is_(None),
        )
    )
    row = (await db.execute(query)).scalar_one_or_none()
    if row is None:
        return AttachmentDeleteResult(status="already_deleted")
    row.detached_at = datetime.now(timezone.utc)
    logger.info(
        "attachment_token_revoked",
        extra={
            "agent_id": agent_id,
            "label": row.label,
            "daemon_id": str(daemon_id),
            "old_token": ipc_session_token,
            "new_token": None,
            "reason": "explicit_delete",
        },
    )
    return AttachmentDeleteResult(status="deleted")


# ───────────────────────────────────────────────────────────────────────
# reconcile_attachments — POST /v1/agents/reconcile-attachments
# ───────────────────────────────────────────────────────────────────────


async def reconcile_attachments(
    db: AsyncSession,
    *,
    daemon_id: UUID,
    attachments: List[AttachmentReconcileEntry],
) -> AttachmentReconcileResult:
    """Single atomic transaction: UPSERT reported attachments + downgrade
    unmentioned-for-this-daemon rows to ``transport='poll'``.

    Conservative downgrade-not-delete per CMA Q-G lean — daemon might be
    flapping; first absence shouldn't lose the row. Daily cleanup job
    deletes ``transport='poll'`` rows >24h stale.
    """
    now = datetime.now(timezone.utc)
    reported_tokens = {a.ipc_session_token for a in attachments}

    upserted_count = 0
    for entry in attachments:
        # Try update existing row (matched by daemon_id + label + token).
        result = await db.execute(
            update(AgentLiveSession)
            .where(
                and_(
                    AgentLiveSession.daemon_id == daemon_id,
                    AgentLiveSession.label == entry.label,
                    AgentLiveSession.ipc_session_token == entry.ipc_session_token,
                    AgentLiveSession.detached_at.is_(None),
                )
            )
            .values(
                transport="ipc",
                last_reconciled_at=now,
                task_name=entry.task_name,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount > 0:
            upserted_count += result.rowcount
            continue
        # No matching row → would-be-INSERT path. Phase 1 reconcile is
        # idempotent-on-existing only; daemon should have called POST
        # /attachments first. Log + skip for forensics.
        logger.info(
            "reconcile_unknown_attachment",
            extra={
                "daemon_id": str(daemon_id),
                "label": entry.label,
                "token": entry.ipc_session_token,
                "task_name": entry.task_name,
            },
        )

    # Downgrade unmentioned rows for this daemon to transport='poll'.
    downgrade_query = update(AgentLiveSession).where(
        and_(
            AgentLiveSession.daemon_id == daemon_id,
            AgentLiveSession.transport == "ipc",
            AgentLiveSession.detached_at.is_(None),
            AgentLiveSession.ipc_session_token.notin_(reported_tokens)
            if reported_tokens
            else (True == True),  # noqa: E712 — empty reconcile: downgrade ALL
        )
    ).values(transport="poll").execution_options(synchronize_session=False)
    downgrade_result = await db.execute(downgrade_query)
    downgraded_count = downgrade_result.rowcount or 0

    return AttachmentReconcileResult(
        upserted_count=upserted_count, downgraded_count=downgraded_count
    )
