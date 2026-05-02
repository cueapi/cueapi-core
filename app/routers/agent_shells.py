"""Agent shell management endpoints (PR-5a Multi-shell same-agent claims).

* ``POST   /v1/agents/{ref}/shells``                 — register a new shell
* ``GET    /v1/agents/{ref}/shells``                 — list live + offline shells
* ``DELETE /v1/agents/{ref}/shells/{shell_id}``      — unregister a shell
* ``POST   /v1/agents/{ref}/shells/{shell_id}/heartbeat`` — bump last_heartbeat_at

Auth: same as the rest of the agents surface (``get_current_user``).
The agent referenced by ``{ref}`` must be owned by the authenticated
user — cross-user shell registration is rejected at this layer.

Push delivery service-layer integration (fan-out across all live
shells) lands in a follow-up; this PR establishes the table + API
surface.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthenticatedUser, get_current_user
from app.database import get_db
from app.models import Agent, AgentShell
from app.services.agent_service import resolve_address
from app.utils.ids import generate_agent_shell_id, generate_webhook_secret
from app.utils.url_validation import validate_callback_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/agents", tags=["agents"])


# ─── Schemas ────────────────────────────────────────────────────────


class ShellCreate(BaseModel):
    webhook_url: Optional[str] = Field(None, max_length=2048)
    label: Optional[str] = Field(None, max_length=128)


class ShellResponse(BaseModel):
    id: str
    agent_id: str
    webhook_url: Optional[str]
    webhook_secret: Optional[str] = None  # included on create only
    label: Optional[str]
    status: str
    last_heartbeat_at: datetime
    registered_at: datetime


class ShellListResponse(BaseModel):
    shells: List[ShellResponse]
    count: int


# ─── Helpers ────────────────────────────────────────────────────────


async def _resolve_owned_agent(
    db: AsyncSession, ref: str, user: AuthenticatedUser
) -> Agent:
    agent = await resolve_address(db, ref)
    if str(agent.user_id) != str(user.id):
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "agent_not_found", "message": "Agent not found", "status": 404}},
        )
    if agent.deleted_at is not None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "agent_deleted", "message": "Agent is soft-deleted", "status": 404}},
        )
    return agent


def _to_response(shell: AgentShell, *, include_secret: bool = False) -> ShellResponse:
    return ShellResponse(
        id=shell.id,
        agent_id=shell.agent_id,
        webhook_url=shell.webhook_url,
        webhook_secret=shell.webhook_secret if include_secret else None,
        label=shell.label,
        status=shell.status,
        last_heartbeat_at=shell.last_heartbeat_at,
        registered_at=shell.registered_at,
    )


# ─── Endpoints ─────────────────────────────────────────────────────


@router.post("/{ref}/shells", response_model=ShellResponse, status_code=201)
async def register_shell(
    ref: str,
    body: ShellCreate,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Register a new shell for the referenced agent. Multiple
    concurrent shells on the same agent are allowed — that's the
    point of this endpoint.
    """
    agent = await _resolve_owned_agent(db, ref, user)

    webhook_secret: Optional[str] = None
    webhook_url: Optional[str] = None
    if body.webhook_url:
        from app.config import settings as _settings
        is_valid, error_msg = validate_callback_url(str(body.webhook_url), _settings.ENV)
        if not is_valid:
            raise HTTPException(
                status_code=400,
                detail={"error": {"code": "invalid_webhook_url", "message": error_msg, "status": 400}},
            )
        webhook_url = str(body.webhook_url)
        webhook_secret = generate_webhook_secret()

    shell = AgentShell(
        id=generate_agent_shell_id(),
        agent_id=agent.id,
        user_id=agent.user_id,
        webhook_url=webhook_url,
        webhook_secret=webhook_secret,
        label=body.label,
        status="online",
    )
    db.add(shell)
    await db.commit()
    await db.refresh(shell)

    logger.info(
        "Agent shell registered",
        extra={
            "event_type": "agent_shell_registered",
            "agent_id": agent.id,
            "shell_id": shell.id,
        },
    )
    # Include webhook_secret on the create response so the integrator
    # can save it (it's never readable again afterward).
    return _to_response(shell, include_secret=True)


@router.get("/{ref}/shells", response_model=ShellListResponse)
async def list_shells(
    ref: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List every shell registered for the referenced agent. Returns
    both online and offline shells. webhook_secret is NEVER included
    in list responses (only in the create response)."""
    agent = await _resolve_owned_agent(db, ref, user)
    result = await db.execute(
        select(AgentShell)
        .where(AgentShell.agent_id == agent.id)
        .order_by(AgentShell.registered_at.desc())
    )
    shells = list(result.scalars().all())
    return ShellListResponse(
        shells=[_to_response(s) for s in shells],
        count=len(shells),
    )


@router.delete("/{ref}/shells/{shell_id}", status_code=204)
async def unregister_shell(
    ref: str,
    shell_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Hard-delete a shell. Use when the process knows it's shutting
    down (graceful exit). Stale shells are also pruned by the periodic
    cleanup task in worker/message_cleanup.py based on
    last_heartbeat_at."""
    agent = await _resolve_owned_agent(db, ref, user)
    result = await db.execute(
        delete(AgentShell)
        .where(
            AgentShell.id == shell_id,
            AgentShell.agent_id == agent.id,
        )
        .returning(AgentShell.id)
    )
    deleted = result.scalar_one_or_none()
    await db.commit()
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "shell_not_found", "message": "Shell not found on this agent", "status": 404}},
        )
    return None


@router.post("/{ref}/shells/{shell_id}/heartbeat", response_model=ShellResponse)
async def heartbeat_shell(
    ref: str,
    shell_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bump ``last_heartbeat_at`` to NOW. Idempotent. Push delivery
    skips shells whose last heartbeat is older than
    MESSAGE_DELIVERY_STALE_AFTER_SECONDS — heartbeat regularly to
    stay in the live set.

    Returns the updated shell. ``webhook_secret`` is NEVER returned
    on heartbeat (use the create response or the rotation endpoint
    if you lose it)."""
    agent = await _resolve_owned_agent(db, ref, user)
    now = datetime.now(timezone.utc)
    result = await db.execute(
        update(AgentShell)
        .where(
            AgentShell.id == shell_id,
            AgentShell.agent_id == agent.id,
        )
        .values(last_heartbeat_at=now, status="online")
        .returning(AgentShell)
    )
    shell = result.scalar_one_or_none()
    await db.commit()
    if not shell:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "shell_not_found", "message": "Shell not found on this agent", "status": 404}},
        )
    return _to_response(shell)
