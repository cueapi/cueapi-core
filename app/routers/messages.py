"""Message primitive router — POST /v1/messages plus per-message endpoints.

Spec: `https://trydock.ai/mike/cueapi-messaging-primitive-v1-sp` §3 (Message primitive) +
§8 (Idempotency-Key).

The router has two surface areas:

* ``POST /v1/messages`` — send. Caller's "from agent" is identified
  via the ``X-Cueapi-From-Agent`` header (opaque agent_id or slug-form
  per §6 / §13 D11). v2 may infer this from API-key-bound default
  agents; v1 is explicit on the request surface.
* ``GET /v1/messages/{id}`` — read.
* ``POST /v1/messages/{id}/read`` — mark read. Idempotent.
* ``POST /v1/messages/{id}/ack`` — acknowledge. Terminal.

Inbox endpoints (``GET /v1/agents/{ref}/inbox`` etc.) live in Phase
12.1.4 — they expose the recipient view of the same Message rows
this router creates.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthenticatedUser, get_current_user
from app.database import get_db
from app.schemas.message import (
    MessageCreate,
    MessageListResponse,
    MessageResponse,
    StateTransitionResponse,
)
from app.services.agent_service import resolve_address
from app.services.message_service import (
    create_message,
    get_message_for_user,
    mark_acked,
    mark_read,
    to_response_dict,
)
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/v1/messages", tags=["messages"])


@router.post("")
async def send_message(
    body: MessageCreate,
    request: Request,
    x_cueapi_from_agent: Optional[str] = Header(default=None, alias="X-Cueapi-From-Agent"),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Send a message.

    Required header: ``X-Cueapi-From-Agent`` — the sender agent's
    opaque ID or slug-form. Must be owned by the caller.

    Optional header: ``Idempotency-Key`` (≤255 chars). Same key + same
    body within 24h → returns existing message with 200 instead of
    201. Same key + different body → 409 ``idempotency_key_conflict``.
    """
    if not x_cueapi_from_agent:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "missing_from_agent", "message": "X-Cueapi-From-Agent header is required", "status": 400}},
        )

    # Resolve from_agent first (caller-owned check is in the service layer).
    from_agent = await resolve_address(db, x_cueapi_from_agent)

    if idempotency_key and len(idempotency_key) > 255:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "invalid_idempotency_key", "message": "Idempotency-Key must be ≤255 characters", "status": 400}},
        )

    msg, was_dedup_hit, priority_downgraded = await create_message(
        db,
        user,
        to=body.to,
        body=body.body,
        subject=body.subject,
        reply_to=body.reply_to,
        priority=body.priority,
        expects_reply=body.expects_reply,
        reply_to_agent=body.reply_to_agent,
        metadata=body.metadata,
        idempotency_key=idempotency_key,
        from_agent=from_agent,
    )
    status_code = 200 if was_dedup_hit else 201
    headers = {}
    if priority_downgraded:
        # §7.3 — receiver-pair priority>3 limit downgraded the message
        # to priority=3. Surface the signal so senders can detect and
        # adapt without parsing message body.
        headers["X-CueAPI-Priority-Downgraded"] = "true"
    return JSONResponse(
        status_code=status_code,
        content=MessageResponse(**to_response_dict(msg)).model_dump(mode="json"),
        headers=headers,
    )


@router.get("/{msg_id}", response_model=MessageResponse)
async def get_message_endpoint(
    msg_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    msg = await get_message_for_user(db, user, msg_id)
    return MessageResponse(**to_response_dict(msg))


@router.post("/{msg_id}/read", response_model=StateTransitionResponse)
async def mark_read_endpoint(
    msg_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mark a message as read.

    Idempotent: calling on already-`read` returns 200 unchanged.
    Returns 409 if the message is in a terminal state (`acked`,
    `expired`).
    """
    msg = await mark_read(db, user, msg_id)
    return StateTransitionResponse(
        delivery_state=msg.delivery_state,
        read_at=msg.read_at,
    )


@router.post("/{msg_id}/ack", response_model=StateTransitionResponse)
async def mark_acked_endpoint(
    msg_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Acknowledge. Terminal — no further state transitions allowed."""
    msg = await mark_acked(db, user, msg_id)
    return StateTransitionResponse(
        delivery_state=msg.delivery_state,
        acked_at=msg.acked_at,
    )
