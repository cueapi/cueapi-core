from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthenticatedUser, get_current_user
from app.database import get_db
from app.models.user import User
from app.routers.auth_routes import _send_webhook_secret_regeneration_email
from app.utils.ids import generate_webhook_secret

router = APIRouter(prefix="/v1/auth", tags=["auth"])


class WebhookSecretResponse(BaseModel):
    webhook_secret: str


class WebhookSecretRegenerateResponse(BaseModel):
    webhook_secret: str
    previous_secret_revoked: bool


@router.get("/webhook-secret", response_model=WebhookSecretResponse)
async def get_webhook_secret(
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Retrieve the current webhook signing secret."""
    result = await db.execute(
        select(User.webhook_secret).where(User.id == user.id)
    )
    row = result.fetchone()
    if not row or not row.webhook_secret:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "no_webhook_secret", "message": "No webhook secret found", "status": 404}},
        )
    return WebhookSecretResponse(webhook_secret=row.webhook_secret)


@router.post("/webhook-secret/regenerate", response_model=WebhookSecretRegenerateResponse)
async def regenerate_webhook_secret(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Regenerate webhook signing secret. Old secret is instantly revoked."""
    if request.headers.get("x-confirm-destructive") != "true":
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "confirmation_required", "message": "This action is destructive. Set X-Confirm-Destructive: true header to confirm.", "status": 400}},
        )
    new_secret = generate_webhook_secret()

    await db.execute(
        update(User)
        .where(User.id == user.id)
        .values(webhook_secret=new_secret)
    )
    await db.commit()

    # Send email notification
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    _send_webhook_secret_regeneration_email(user.email, timestamp)

    return WebhookSecretRegenerateResponse(
        webhook_secret=new_secret,
        previous_secret_revoked=True,
    )
