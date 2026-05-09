"""Agent Live Sessions — per-label Live attachment endpoints.

Replaces the multi-shell webhook-target surface (``agent_shells``) with
a per-session attachment model. Each row in ``agent_live_sessions``
represents one attached Live session for an agent; the row marked
``is_default=true`` is the routing target for senders who fire
without specifying a label.

Endpoints:

* ``POST   /v1/agents/{ref}/live-sessions`` — register a new session
* ``GET    /v1/agents/{ref}/live-sessions`` — list active (or include
  detached via ``?include_detached=true``)
* ``DELETE /v1/agents/{ref}/live-sessions/{label}`` — soft-detach
  (sets ``detached_at = now()``; row stays in audit trail)
* ``PATCH  /v1/agents/{ref}/live-sessions/{label}`` — flip
  ``is_default`` atomically OR rotate ``session_token`` (Crockford-
  base32 ULID for cmotigtnx attestation)

Heartbeat (``POST .../live-sessions/{label}/heartbeat``) and
live-claim attestation (``POST /v1/executions/{id}/live-claim``) are
scoped for follow-up PRs alongside the consumer-side wire-through;
see CHANGELOG entry under ``[Unreleased] > Upcoming breaking change``.

Auth: same as the rest of the agents surface
(``get_current_user``). Cross-user registration is rejected by
``resolve_address``.

Constraints (DB-enforced via partial unique indexes in migration 026):

* ``cue_id`` is globally unique
* ``label`` is unique per agent among active sessions
* at most one ``is_default=true`` per agent
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import case, false, select, true, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthenticatedUser, get_current_user
from app.database import get_db
from app.models import Agent, AgentLiveSession
from app.services.agent_service import resolve_address

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/agents", tags=["agents"])


# ─── Schemas (inline, OSS pattern matches old agent_shells.py) ─────


class LiveSessionRegisterRequest(BaseModel):
    label: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description=(
            "Per-agent label for this session. Default attach uses ``main``."
        ),
    )
    cue_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Globally-unique cue ID. Pattern: ``cue_<12 alphanum>``.",
    )
    task_name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description=(
            "Canonical handler binding — must match ``payload.task`` "
            "verbatim on incoming fires."
        ),
    )
    is_default: bool = Field(
        default=False,
        description=(
            "Mark as default routing target when sender fires without a "
            "label. At most one ``is_default=true`` per agent. Use PATCH "
            "to flip atomically."
        ),
    )
    monitor_version: Optional[str] = Field(
        default=None,
        max_length=64,
        description=(
            "Optional client-provided Monitor version. Format convention: "
            "semver-style (``v2.1.0``) for sortable string compare; "
            "commit-SHA fallback acceptable but mixed formats require "
            "caution when comparing."
        ),
    )
    session_token: Optional[str] = Field(
        default=None,
        max_length=80,
        description=(
            "cmotigtnx attestation ULID minted by the Monitor at startup. "
            "Cross-referenced by ``POST /v1/executions/{id}/live-claim`` "
            "(scoped for follow-up PR)."
        ),
    )


class LiveSessionEntry(BaseModel):
    """Wire-shape for register / list / patch / detach responses."""

    label: str
    cue_id: str
    task_name: str
    is_default: bool
    attached: bool = Field(
        ...,
        description="True when ``detached_at IS NULL``. Computed server-side.",
    )
    heartbeat_age_sec: Optional[int] = None
    last_claim_at: Optional[datetime] = None
    last_claim_age_sec: Optional[int] = None
    monitor_version: Optional[str] = None
    attached_at: Optional[datetime] = None
    session_token: Optional[str] = None


class LiveSessionUpdateRequest(BaseModel):
    is_default: Optional[bool] = Field(
        default=None,
        description=(
            "Flip is_default. Atomic against partial unique index — "
            "old default flips to false in the same UPDATE statement."
        ),
    )
    session_token: Optional[str] = Field(
        default=None,
        max_length=80,
        description="Rotate the cmotigtnx attestation ULID without detaching.",
    )


# ─── Helpers ────────────────────────────────────────────────────────


def _http_error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"error": {"code": code, "message": message, "status": status}},
    )


def _to_entry(row: AgentLiveSession, *, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    heartbeat_age = None
    if row.last_heartbeat is not None:
        heartbeat_age = max(0, int((now - row.last_heartbeat).total_seconds()))
    last_claim_age = None
    if row.last_claim_at is not None:
        last_claim_age = max(0, int((now - row.last_claim_at).total_seconds()))
    return {
        "label": row.label,
        "cue_id": row.cue_id,
        "task_name": row.task_name,
        "is_default": row.is_default,
        "attached": row.detached_at is None,
        "heartbeat_age_sec": heartbeat_age,
        "last_claim_at": row.last_claim_at,
        "last_claim_age_sec": last_claim_age,
        "monitor_version": row.monitor_version,
        "attached_at": row.attached_at,
        "session_token": row.session_token,
    }


async def _get_owned_agent(
    db: AsyncSession, user: AuthenticatedUser, ref: str
) -> Agent:
    """Resolve ``ref`` and verify the authenticated user owns the agent.

    ``resolve_address`` raises 404 if the agent doesn't exist; we then
    enforce cross-user denial here (404, not 403, to avoid leaking
    existence of other users' agents).
    """
    agent = await resolve_address(db, ref)
    if str(agent.user_id) != str(user.id):
        raise _http_error(404, "agent_not_found", f"Agent {ref!r} not found")
    return agent


# ─── Endpoints ──────────────────────────────────────────────────────


@router.post(
    "/{ref}/live-sessions",
    response_model=LiveSessionEntry,
    status_code=201,
)
async def register_live_session(
    ref: str,
    body: LiveSessionRegisterRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LiveSessionEntry:
    """Register a new Live session for the agent.

    Per-row registration: the calling session is the single-writer for
    its row. Composite registration (one call registers all of an
    agent's sessions) was rejected during design — eliminates the
    "which session writes the canonical full list?" concurrency
    question.

    Constraints (returned as 409 ``conflict``):

    * cue_id collision (across all agents)
    * label collision (within this agent's active sessions)
    * is_default=true while another session of this agent already
      holds default
    """
    agent = await _get_owned_agent(db, user, ref)

    now = datetime.now(timezone.utc)
    row = AgentLiveSession(
        agent_id=agent.id,
        label=body.label,
        cue_id=body.cue_id,
        task_name=body.task_name,
        is_default=body.is_default,
        attached_at=now,
        last_heartbeat=now,
        monitor_version=body.monitor_version,
        session_token=body.session_token,
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise _http_error(
            409,
            "live_session_conflict",
            f"Could not register session: {exc.orig}",
        )
    await db.refresh(row)

    logger.info(
        "agent_live_session_registered",
        extra={
            "event_type": "agent_live_session_registered",
            "agent_id": agent.id,
            "label": body.label,
            "cue_id": body.cue_id,
            "is_default": body.is_default,
        },
    )
    return LiveSessionEntry(**_to_entry(row, now=now))


@router.get(
    "/{ref}/live-sessions",
    response_model=List[LiveSessionEntry],
)
async def list_live_sessions(
    ref: str,
    include_detached: bool = Query(
        default=False,
        description=(
            "Include soft-detached sessions in the response. Default false "
            "(only active). Set true for audit-trail / claim-history view."
        ),
    ),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> List[LiveSessionEntry]:
    """List Live sessions for this agent.

    Default returns only active (``detached_at IS NULL``). Pass
    ``?include_detached=true`` to surface the audit trail.
    """
    agent = await _get_owned_agent(db, user, ref)

    stmt = select(AgentLiveSession).where(AgentLiveSession.agent_id == agent.id)
    if not include_detached:
        stmt = stmt.where(AgentLiveSession.detached_at.is_(None))
    stmt = stmt.order_by(AgentLiveSession.created_at.desc())
    result = await db.execute(stmt)
    rows = result.scalars().all()

    now = datetime.now(timezone.utc)
    return [LiveSessionEntry(**_to_entry(r, now=now)) for r in rows]


@router.delete(
    "/{ref}/live-sessions/{label}",
    response_model=LiveSessionEntry,
)
async def detach_live_session(
    ref: str,
    label: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LiveSessionEntry:
    """Soft-detach the active session with this label.

    Sets ``detached_at = now()``; the row stays in the audit trail.
    Re-registering with the same label is then allowed (creates a
    fresh row).
    """
    agent = await _get_owned_agent(db, user, ref)

    now = datetime.now(timezone.utc)
    stmt = (
        update(AgentLiveSession)
        .where(
            AgentLiveSession.agent_id == agent.id,
            AgentLiveSession.label == label,
            AgentLiveSession.detached_at.is_(None),
        )
        .values(detached_at=now, is_default=False)
        .returning(AgentLiveSession)
    )
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()
    if row is None:
        raise _http_error(
            404,
            "live_session_not_found",
            f"No active session with label {label!r} for agent {ref!r}",
        )
    await db.commit()
    await db.refresh(row)

    logger.info(
        "agent_live_session_detached",
        extra={
            "event_type": "agent_live_session_detached",
            "agent_id": agent.id,
            "label": label,
        },
    )
    return LiveSessionEntry(**_to_entry(row, now=now))


@router.patch(
    "/{ref}/live-sessions/{label}",
    response_model=LiveSessionEntry,
)
async def patch_live_session(
    ref: str,
    label: str,
    body: LiveSessionUpdateRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LiveSessionEntry:
    """Update a session — flip ``is_default`` (atomic) or rotate
    ``session_token``.

    is_default flips use a single-statement UPDATE that re-evaluates
    is_default for every active session of this agent. Old default
    flips to false in the same statement as new default flips to
    true; no zero-or-two-defaults window.

    Other fields (cue_id, task_name) are immutable post-attach. Detach
    and re-register to change them.
    """
    agent = await _get_owned_agent(db, user, ref)

    if body.is_default is None and body.session_token is None:
        raise _http_error(
            400,
            "no_mutable_fields",
            (
                "PATCH requires at least one mutable field set: "
                "``is_default=true`` (atomic flip) or "
                "``session_token=<ulid>`` (rotation)."
            ),
        )

    if body.is_default is False:
        raise _http_error(
            400,
            "invalid_default_flip",
            (
                "is_default=false on its own is a no-op; flip another "
                "session to is_default=true (the swap is atomic), or "
                "DELETE this session to detach it."
            ),
        )

    # Rotate session_token first (independent of default-flip).
    if body.session_token is not None:
        stmt = (
            update(AgentLiveSession)
            .where(
                AgentLiveSession.agent_id == agent.id,
                AgentLiveSession.label == label,
                AgentLiveSession.detached_at.is_(None),
            )
            .values(session_token=body.session_token)
            .returning(AgentLiveSession)
        )
        result = await db.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            raise _http_error(
                404,
                "live_session_not_found",
                f"No active session with label {label!r} for agent {ref!r}",
            )

    # Atomic is_default flip.
    if body.is_default is True:
        stmt = (
            update(AgentLiveSession)
            .where(
                AgentLiveSession.agent_id == agent.id,
                AgentLiveSession.detached_at.is_(None),
            )
            .values(
                is_default=case(
                    (AgentLiveSession.label == label, true()),
                    else_=false(),
                )
            )
            .returning(AgentLiveSession)
        )
        result = await db.execute(stmt)
        rows = result.scalars().all()
        target = next((r for r in rows if r.label == label), None)
        if target is None:
            await db.rollback()
            raise _http_error(
                404,
                "live_session_not_found",
                f"No active session with label {label!r} for agent {ref!r}",
            )

    await db.commit()

    # Re-fetch the target row for the return shape.
    final_stmt = select(AgentLiveSession).where(
        AgentLiveSession.agent_id == agent.id,
        AgentLiveSession.label == label,
        AgentLiveSession.detached_at.is_(None),
    )
    final_result = await db.execute(final_stmt)
    final_row = final_result.scalar_one_or_none()
    if final_row is None:
        raise _http_error(
            404,
            "live_session_not_found",
            f"No active session with label {label!r} for agent {ref!r}",
        )

    logger.info(
        "agent_live_session_patched",
        extra={
            "event_type": "agent_live_session_patched",
            "agent_id": agent.id,
            "label": label,
            "set_default": body.is_default,
            "rotated_token": body.session_token is not None,
        },
    )
    return LiveSessionEntry(**_to_entry(final_row))
