"""Item B Phase 1 — endpoints for daemon-IPC attachment lifecycle.

Live-delivery-v3 substrate primitive (cf. https://trydock.ai/mike/live-delivery-v3-build-hub).
Mike Q-B ratify locked the ASYNC fire-accept dispatcher path 2026-05-12 ~00:38Z.

Three endpoints:

* ``POST /v1/agents/<ref>/attachments`` — daemon attaches a Live session.
* ``DELETE /v1/agents/<ref>/attachments/<token>`` — daemon revokes.
* ``POST /v1/agents/reconcile-attachments`` — daemon boot-reconcile.

Daemon identity via the ``X-CueAPI-Daemon-Id`` header (UUID; mismatch
with body.daemon_id on reconcile → 400).

Router stays thin — service layer (``app/services/ipc_attachment_service.py``)
does the heavy lifting so pytest-cov traces branches per CLAUDE.md ASGI
dispatch discipline.
"""
from __future__ import annotations

from typing import Optional, Tuple
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthenticatedUser, get_current_user
from app.database import get_db
from app.models.agent import Agent
from app.schemas.ipc_attachment import (
    AttachmentCreate,
    AttachmentDeleteIdempotent,
    AttachmentExistsError,
    AttachmentReconcileRequest,
    AttachmentReconcileResponse,
    AttachmentResponse,
)
from app.services.ipc_attachment_service import (
    create_attachment,
    delete_attachment,
    reconcile_attachments,
)


router = APIRouter(prefix="/v1/agents", tags=["ipc-attachments"])
# Reconcile is daemon-scoped (not per-agent in the URL), so register on a
# second router at the same prefix to keep routing clean.
reconcile_router = APIRouter(prefix="/v1/agents", tags=["ipc-attachments"])

_DAEMON_ID_HEADER = "X-CueAPI-Daemon-Id"


# ───────────────────────────────────────────────────────────────────────
# Pure helpers — extracted per CLAUDE.md ASGI dispatch coverage discipline
# ───────────────────────────────────────────────────────────────────────


def _parse_daemon_id(
    raw: Optional[str],
) -> Tuple[Optional[UUID], Optional[JSONResponse]]:
    """Validate the X-CueAPI-Daemon-Id header.

    Returns ``(uuid, None)`` on success, ``(None, error_response)`` on
    failure. Module-level helper so pytest-cov traces branches directly
    (the ASGI dispatch wrap on the endpoint handlers hides them otherwise).
    """
    if not raw:
        return None, JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": "missing_daemon_id",
                    "message": (
                        "X-CueAPI-Daemon-Id header is required on this "
                        "endpoint. Generate a UUID at daemon install time "
                        "and send it on every attach/reconcile/delete."
                    ),
                    "status": 400,
                }
            },
        )
    try:
        return UUID(raw.strip()), None
    except (ValueError, AttributeError):
        return None, JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": "invalid_daemon_id",
                    "message": "X-CueAPI-Daemon-Id must be a valid UUID.",
                    "status": 400,
                }
            },
        )


async def _resolve_agent_ref(
    db: AsyncSession, ref: str, user: AuthenticatedUser
) -> Tuple[Optional[Agent], Optional[JSONResponse]]:
    """Resolve agent_ref to an Agent owned by the caller.

    Phase 1 supports opaque ``agt_xxx`` only; slug-form deferred (daemon
    already has the opaque ID from earlier POST /v1/agents response).
    Returns ``(agent, None)`` or ``(None, error_response)``.
    """
    if ref.startswith("agt_"):
        row = (
            await db.execute(
                select(Agent).where(Agent.id == ref, Agent.user_id == user.id)
            )
        ).scalar_one_or_none()
        if row is None:
            return None, JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "agent_not_found",
                        "message": f"Agent {ref} not found",
                        "status": 404,
                    }
                },
            )
        return row, None
    return None, JSONResponse(
        status_code=400,
        content={
            "error": {
                "code": "invalid_agent_ref",
                "message": (
                    "Pass the agent's opaque ID (agt_xxx) on this endpoint, "
                    "not slug-form."
                ),
                "status": 400,
            }
        },
    )


# ───────────────────────────────────────────────────────────────────────
# POST /v1/agents/<ref>/attachments
# ───────────────────────────────────────────────────────────────────────


@router.post("/{ref}/attachments", status_code=201)
async def post_attachment(
    ref: str,
    body: AttachmentCreate,
    x_cueapi_daemon_id: Optional[str] = Header(default=None, alias=_DAEMON_ID_HEADER),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Daemon attaches an IPC Live session.

    Idempotency:
    - Same-(agent, label, daemon_id) reattach → REPLACE (new token issued;
      old row detached; old token returns 401 on subsequent use).
    - Cross-daemon collision on (agent, label) → 409 with
      ``existing_daemon_id`` + ``existing_last_reconciled_at`` so daemon
      can decide whether to escalate or DELETE+re-POST.
    """
    daemon_id, err = _parse_daemon_id(x_cueapi_daemon_id)
    if err is not None:
        return err

    agent, ag_err = await _resolve_agent_ref(db, ref, user)
    if ag_err is not None:
        return ag_err
    assert agent is not None  # narrowed by ag_err is None
    assert daemon_id is not None

    result = await create_attachment(
        db,
        agent_id=agent.id,
        label=body.label,
        task_name=body.task_name,
        ipc_session_token=body.ipc_session_token,
        daemon_id=daemon_id,
        attached_at=body.attached_at,
        monitor_version=body.monitor_version,
    )

    if result.status == "conflict_cross_daemon":
        assert result.existing is not None
        existing = result.existing
        err_body = AttachmentExistsError(
            existing_token=existing.ipc_session_token or "",
            existing_daemon_id=str(existing.daemon_id) if existing.daemon_id else "",
            existing_attached_at=existing.attached_at,
            existing_last_reconciled_at=existing.last_reconciled_at,
        ).model_dump(mode="json")
        return JSONResponse(status_code=409, content={"error": err_body})

    await db.commit()
    assert result.row is not None
    row = result.row
    return AttachmentResponse(
        id=str(row.id),
        agent_id=row.agent_id,
        label=row.label,
        task_name=row.task_name,
        transport=row.transport,
        ipc_session_token=row.ipc_session_token or "",
        daemon_id=str(row.daemon_id) if row.daemon_id else "",
        attached_at=row.attached_at,
        last_reconciled_at=row.last_reconciled_at,
        supersedes_token=result.supersedes_token,
    ).model_dump(mode="json")


# ───────────────────────────────────────────────────────────────────────
# DELETE /v1/agents/<ref>/attachments/<token>
# ───────────────────────────────────────────────────────────────────────


@router.delete("/{ref}/attachments/{token}")
async def delete_attachment_endpoint(
    ref: str,
    token: str,
    x_cueapi_daemon_id: Optional[str] = Header(default=None, alias=_DAEMON_ID_HEADER),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Idempotent DELETE by token, scoped to caller's daemon_id.

    - First-time: 204 No Content
    - Idempotent hit: 200 with ``{"deleted": false, "reason": "already_deleted"}``
      so daemon-side debugging can distinguish 'I won the race' from
      'someone else cleaned up'.
    """
    daemon_id, err = _parse_daemon_id(x_cueapi_daemon_id)
    if err is not None:
        return err

    agent, ag_err = await _resolve_agent_ref(db, ref, user)
    if ag_err is not None:
        return ag_err
    assert agent is not None
    assert daemon_id is not None

    result = await delete_attachment(
        db,
        agent_id=agent.id,
        ipc_session_token=token,
        daemon_id=daemon_id,
    )
    await db.commit()

    if result.status == "deleted":
        return Response(status_code=204)
    return JSONResponse(
        status_code=200,
        content=AttachmentDeleteIdempotent().model_dump(),
    )


# ───────────────────────────────────────────────────────────────────────
# POST /v1/agents/reconcile-attachments
# ───────────────────────────────────────────────────────────────────────


@reconcile_router.post("/reconcile-attachments", status_code=200)
async def post_reconcile_attachments(
    body: AttachmentReconcileRequest,
    x_cueapi_daemon_id: Optional[str] = Header(default=None, alias=_DAEMON_ID_HEADER),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Daemon reports full local attachment view; server reconciles.

    Atomic transaction:
    1. UPSERT each reported attachment as ``transport='ipc'`` with
       ``last_reconciled_at=now()``.
    2. UPDATE all rows for this daemon_id NOT in batch → ``transport='poll'``
       (conservative downgrade per CMA Q-G lean).
    3. Daily cleanup job (separate) deletes ``transport='poll'`` rows >24h
       stale.

    Daemon scoping enforced via X-CueAPI-Daemon-Id header AND body.daemon_id
    must match — defends against transport-layer spoof or body-tampering
    mismatches.
    """
    daemon_id, err = _parse_daemon_id(x_cueapi_daemon_id)
    if err is not None:
        return err
    assert daemon_id is not None

    if daemon_id != body.daemon_id:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": "daemon_id_mismatch",
                    "message": (
                        "X-CueAPI-Daemon-Id header and body.daemon_id must "
                        "match. Both identify the same calling daemon."
                    ),
                    "status": 400,
                }
            },
        )

    # `user` dependency is included so the call is auth-required even
    # though the reconcile-attachments endpoint doesn't currently filter
    # by user_id (daemon_id is the primary scope key). The auth check
    # gates calls behind a valid API key; daemon_id provides identity
    # within the user's tenant.
    _ = user  # explicit no-op so linters don't strip the dependency

    result = await reconcile_attachments(
        db,
        daemon_id=daemon_id,
        attachments=body.attachments,
    )
    await db.commit()

    return AttachmentReconcileResponse(
        daemon_id=str(daemon_id),
        reconciled_at=body.reconciled_at,
        upserted_count=result.upserted_count,
        downgraded_count=result.downgraded_count,
    ).model_dump(mode="json")
