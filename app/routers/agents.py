"""Identity primitive router — POST/GET/PATCH/DELETE /v1/agents.

Spec: `https://trydock.ai/mike/cueapi-messaging-primitive-v1-sp` §2 (Identity primitive) endpoints +
§13 D11 (slug-form delimiter `agent_slug@user_slug`).

Auth: every endpoint depends on ``get_current_user``. Per-user scoping
is enforced in the service layer (``get_agent_owned``) — soft-deleted
agents are visible only when ``?include_deleted=true``.

Webhook-secret semantics:

* ``webhook_secret`` is returned **inline** on ``POST /v1/agents``
  responses (one-shot reveal at create time). All other read paths
  omit it. Use the dedicated retrieval endpoint to get it back, and
  the rotation endpoint to mint a fresh one.
* ``POST /v1/agents/{ref}/webhook-secret/regenerate`` requires the
  ``X-Confirm-Destructive: true`` header — same pattern the
  user-level ``POST /v1/auth/key/regenerate`` and
  ``POST /v1/auth/webhook-secret/regenerate`` already use.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthenticatedUser, get_current_user
from app.database import get_db
from app.schemas.agent import (
    AgentCreate,
    AgentListResponse,
    AgentResponse,
    AgentUpdate,
    WebhookSecretResponse,
)
from app.services.agent_service import (
    create_agent,
    get_agent_owned,
    get_webhook_secret,
    list_agents,
    rotate_webhook_secret,
    soft_delete_agent,
    to_response_dict,
    update_agent,
)
from app.services.inbox_service import list_inbox, list_sent
from app.services.message_service import to_response_dict as message_to_response_dict
from app.schemas.message import (
    CountResponse,
    MessageListResponse,
    MessageResponse,
)
from datetime import datetime
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/v1/agents", tags=["agents"])


@router.post("", response_model=AgentResponse, status_code=201)
async def create_agent_endpoint(
    body: AgentCreate,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create an agent.

    On a 201 response the ``webhook_secret`` field is populated when a
    ``webhook_url`` was supplied. Subsequent reads omit the secret.
    """
    agent, plaintext_secret = await create_agent(
        db,
        user,
        slug=body.slug,
        display_name=body.display_name,
        webhook_url=body.webhook_url,
        metadata=body.metadata,
    )
    payload = to_response_dict(agent, include_secret=False)
    # Override secret slot with the freshly-minted plaintext (only set
    # when webhook_url was given). Don't read agent.webhook_secret here;
    # to_response_dict's include_secret=False already nulls it.
    if plaintext_secret is not None:
        payload["webhook_secret"] = plaintext_secret
    return AgentResponse(**payload)


@router.get("", response_model=AgentListResponse)
async def list_agents_endpoint(
    status: Optional[str] = Query(default=None),
    include_deleted: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if status is not None and status not in {"online", "offline", "away"}:
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "invalid_status", "message": "status must be one of: online, offline, away", "status": 400}},
        )
    result = await list_agents(
        db,
        user,
        status=status,
        include_deleted=include_deleted,
        limit=limit,
        offset=offset,
    )
    return AgentListResponse(
        agents=[
            AgentResponse(**to_response_dict(a, include_secret=False))
            for a in result["agents"]
        ],
        total=result["total"],
        limit=result["limit"],
        offset=result["offset"],
    )


@router.get("/{ref}", response_model=AgentResponse)
async def get_agent_endpoint(
    ref: str,
    include_deleted: bool = Query(default=False),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    agent = await get_agent_owned(db, user, ref, include_deleted=include_deleted)
    return AgentResponse(**to_response_dict(agent, include_secret=False))


@router.patch("/{ref}", response_model=AgentResponse)
async def patch_agent_endpoint(
    ref: str,
    body: AgentUpdate,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update display_name, webhook_url, status, metadata.

    ``slug`` is set-once-then-locked (§13 D3) so it isn't on the update
    schema. ``webhook_url`` is tri-state: omitted (no change),
    set-to-null (clears URL + secret), set-to-string (validates SSRF
    and mints/keeps a secret).
    """
    fields_set = body.model_fields_set
    agent = await update_agent(
        db,
        user,
        ref,
        display_name=body.display_name,
        webhook_url_set=("webhook_url" in fields_set),
        webhook_url=body.webhook_url,
        status=body.status,
        metadata=body.metadata,
    )
    return AgentResponse(**to_response_dict(agent, include_secret=False))


@router.delete("/{ref}", status_code=204)
async def delete_agent_endpoint(
    ref: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await soft_delete_agent(db, user, ref)
    return None


@router.get("/{ref}/webhook-secret", response_model=WebhookSecretResponse)
async def get_webhook_secret_endpoint(
    ref: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    secret = await get_webhook_secret(db, user, ref)
    return WebhookSecretResponse(webhook_secret=secret)


@router.post("/{ref}/webhook-secret/regenerate", response_model=WebhookSecretResponse)
async def regenerate_webhook_secret_endpoint(
    ref: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mint a fresh webhook secret. Old secret is dropped immediately.

    Requires the ``X-Confirm-Destructive: true`` header — same pattern
    used for ``POST /v1/auth/key/regenerate`` (regenerate any
    user-or-key-scoped credential).
    """
    if request.headers.get("x-confirm-destructive") != "true":
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "confirmation_required", "message": "This action is destructive. Set X-Confirm-Destructive: true header to confirm.", "status": 400}},
        )
    new_secret = await rotate_webhook_secret(db, user, ref)
    return WebhookSecretResponse(webhook_secret=new_secret)


# ---- Inbox endpoints (Phase 12.1.4) ---------------------------------------
# Per Mike's 2026-04-30 redirection, the inbox endpoint is THE delivery
# surface for poll-based agents (cueapi-Desktop bundled worker, OpenClaw
# Gateway in poll mode, future hosted services without a stable HTTP
# endpoint). Push delivery (Phase 12.1.5) is a v1.5 optimization for
# agents that DO have a stable HTTP endpoint.


@router.get("/{ref}/inbox")
async def get_inbox_endpoint(
    ref: str,
    state: Optional[str] = Query(default=None, description="Comma-separated states; default excludes acked/expired"),
    since: Optional[datetime] = Query(default=None),
    thread_id: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    count_only: bool = Query(default=False),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Recipient view. Atomically transitions queued → delivered for
    surfaced messages. ``count_only=true`` returns ``{count: N}`` for
    inbox-list-badge UX (R3 dock-demo).
    """
    result = await list_inbox(
        db,
        user,
        agent_addr=ref,
        states=state,
        since=since,
        thread_id=thread_id,
        limit=limit,
        offset=offset,
        count_only=count_only,
    )
    if count_only:
        return CountResponse(count=result["count"])
    return MessageListResponse(
        messages=[
            MessageResponse(**message_to_response_dict(m))
            for m in result["messages"]
        ],
        total=result["total"],
        limit=result["limit"],
        offset=result["offset"],
    )


@router.get("/{ref}/sent")
async def get_sent_endpoint(
    ref: str,
    state: Optional[str] = Query(default=None),
    since: Optional[datetime] = Query(default=None),
    thread_id: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    count_only: bool = Query(default=False),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Sender view. No state mutation."""
    result = await list_sent(
        db,
        user,
        agent_addr=ref,
        states=state,
        since=since,
        thread_id=thread_id,
        limit=limit,
        offset=offset,
        count_only=count_only,
    )
    if count_only:
        return CountResponse(count=result["count"])
    return MessageListResponse(
        messages=[
            MessageResponse(**message_to_response_dict(m))
            for m in result["messages"]
        ],
        total=result["total"],
        limit=result["limit"],
        offset=result["offset"],
    )
