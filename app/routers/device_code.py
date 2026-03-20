from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.redis import get_redis
from app.services.device_code_service import (
    create_device_code,
    poll_device_code,
    submit_email,
    verify_token,
)
from app.utils.auth_rate_limit import check_auth_rate_limit
from app.utils.templates import brand_page

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/auth", tags=["auth"])

# Separate router for the HTML verification page (no /v1 prefix)
page_router = APIRouter(tags=["auth-pages"])


class DeviceCodeRequest(BaseModel):
    device_code: str


class DeviceCodeResponse(BaseModel):
    verification_url: str
    expires_in: int


class PollRequest(BaseModel):
    device_code: str


class SubmitEmailRequest(BaseModel):
    device_code: str
    email: EmailStr


@router.post("/device-code", status_code=201)
async def create_device_code_endpoint(
    body: DeviceCodeRequest,
    request: Request = None,
    db: AsyncSession = Depends(get_db),
):
    if not body.device_code or len(body.device_code) < 6 or len(body.device_code) > 128:
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "invalid_device_code", "message": "Device code must be 6-128 characters", "status": 400}},
        )

    # Rate limit: 5 device code creations per IP per hour
    redis = await get_redis()
    client_ip = request.client.host if request and request.client else "unknown"
    allowed = await check_auth_rate_limit(redis, f"auth_dc:{client_ip}", 5, 3600)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail={"error": {"code": "rate_limit_exceeded", "message": "Too many device code requests", "status": 429}},
        )

    result = await create_device_code(db, body.device_code)
    if "error" in result:
        raise HTTPException(status_code=result["error"]["status"], detail=result)
    return result


@router.post("/device-code/poll")
async def poll_device_code_endpoint(
    body: PollRequest,
    request: Request = None,
    db: AsyncSession = Depends(get_db),
):
    # Rate limit: 30 polls per device code per minute
    redis = await get_redis()
    allowed = await check_auth_rate_limit(redis, f"auth_poll:{body.device_code}", 30, 60)
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail={"error": {"code": "rate_limit_exceeded", "message": "Poll rate limit exceeded", "status": 429}},
        )

    result = await poll_device_code(db, body.device_code)
    return result


@router.post("/device-code/submit-email")
async def submit_email_endpoint(
    body: SubmitEmailRequest,
    request: Request = None,
    db: AsyncSession = Depends(get_db),
):
    # Rate limit: 3 per email per hour + 10 per IP per hour
    redis = await get_redis()
    email_allowed = await check_auth_rate_limit(redis, f"auth_ml:{body.email}", 3, 3600)
    if not email_allowed:
        raise HTTPException(
            status_code=429,
            detail={"error": {"code": "rate_limit_exceeded", "message": "Too many magic link requests for this email", "status": 429}},
        )
    client_ip = request.client.host if request and request.client else "unknown"
    ip_allowed = await check_auth_rate_limit(redis, f"auth_ml_ip:{client_ip}", 10, 3600)
    if not ip_allowed:
        raise HTTPException(
            status_code=429,
            detail={"error": {"code": "rate_limit_exceeded", "message": "Too many magic link requests", "status": 429}},
        )

    result = await submit_email(db, body.device_code, body.email)
    if "error" in result:
        raise HTTPException(status_code=result["error"]["status"], detail=result)
    return result


@router.get("/verify", response_class=HTMLResponse)
async def verify_endpoint(
    token: str = Query(...),
    device_code: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    redis = await get_redis()
    result = await verify_token(db, token, device_code, redis)
    if "error" in result:
        return HTMLResponse(
            content=_error_page(result["error"]),
            status_code=400,
        )
    return HTMLResponse(content=_verified_page(result))


# HTML verification page (no auth, served at /auth/device)
@page_router.get("/auth/device", response_class=HTMLResponse)
async def device_page(code: str = Query(...)):
    return HTMLResponse(content=_device_page(code))


# --- HTML Templates ---

def _device_page(device_code: str) -> str:
    body = f"""
        <h1>Sign in to CueAPI</h1>
        <p>Enter your email to receive a verification link.</p>
        <p style="margin-bottom:24px;">Device code: <span class="code">{device_code}</span></p>
        <form id="emailForm">
            <input type="email" id="email" name="email" placeholder="you@example.com" required>
            <button type="submit">Send verification link &rarr;</button>
        </form>
        <div id="result" style="display:none; margin-top: 24px;"></div>
        <script>
            document.getElementById('emailForm').addEventListener('submit', async (e) => {{
                e.preventDefault();
                const email = document.getElementById('email').value;
                const btn = e.target.querySelector('button');
                btn.disabled = true;
                btn.textContent = 'Sending...';
                try {{
                    const resp = await fetch('/v1/auth/device-code/submit-email', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/json'}},
                        body: JSON.stringify({{ device_code: '{device_code}', email }})
                    }});
                    const data = await resp.json();
                    if (data.status === 'email_sent') {{
                        document.getElementById('emailForm').style.display = 'none';
                        const res = document.getElementById('result');
                        res.style.display = 'block';
                        res.innerHTML = '<div style="text-align:center;" class="fade-in">' +
                            '<div class="checkmark-wrap"><svg viewBox="0 0 80 80">' +
                            '<circle class="checkmark-circle" cx="40" cy="40" r="38"/>' +
                            '<path class="checkmark-check" d="M24 42 L34 52 L56 30"/>' +
                            '</svg></div>' +
                            '<h1 style="font-size:22px;margin-bottom:8px;">Check your email</h1>' +
                            '<p>We sent a verification link to <strong style="color:#fafafa;">' + email + '</strong>. Click it to complete login.</p>' +
                            '</div>';
                    }} else {{
                        btn.disabled = false;
                        btn.textContent = 'Send verification link \\u2192';
                        const msg = data.detail?.error?.message || data.error?.message || 'Something went wrong';
                        document.getElementById('result').style.display = 'block';
                        document.getElementById('result').innerHTML = '<div class="result-msg"><p style="color:#EF4444;">' + msg + '</p></div>';
                    }}
                }} catch (err) {{
                    btn.disabled = false;
                    btn.textContent = 'Send verification link \\u2192';
                    document.getElementById('result').style.display = 'block';
                    document.getElementById('result').innerHTML = '<div class="result-msg"><p style="color:#EF4444;">Network error. Please try again.</p></div>';
                }}
            }});
        </script>"""
    return brand_page("CueAPI Login", body)


def _verified_page(verify_result: dict) -> str:
    import json as _json

    email = verify_result.get("email", "")
    session_token = verify_result.get("session_token", "")

    # Always redirect with sessionToken — dashboard exchanges it for a JWT
    auth_data = _json.dumps({"sessionToken": session_token, "email": email})

    # Escape for safe embedding in JS single-quoted string
    auth_data_js = auth_data.replace("'", "\\'")

    body = f"""
        <div style="text-align:center;padding-top:40px;" class="fade-in">
            <div class="checkmark-wrap">
                <svg viewBox="0 0 80 80">
                    <circle class="checkmark-circle" cx="40" cy="40" r="38"/>
                    <path class="checkmark-check" d="M24 42 L34 52 L56 30"/>
                </svg>
            </div>
            <h1>You're in.</h1>
            <p id="status-msg">Redirecting to dashboard...</p>
            <noscript>
                <p>Your email is verified. Return to your original tab.</p>
                <a href="https://dashboard.cueapi.ai" class="btn-primary">Go to Dashboard &rarr;</a>
            </noscript>
        </div>
        <script>
            (function() {{
                var params = encodeURIComponent('{auth_data_js}');
                setTimeout(function() {{
                    window.location.href = 'https://dashboard.cueapi.ai/login#auth=' + params;
                }}, 500);
            }})();
        </script>"""
    return brand_page("CueAPI - Verified", body)


def _error_page(message: str) -> str:
    # Escape HTML in error message
    safe_message = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    body = f"""
        <div style="text-align:center;padding-top:40px;" class="fade-in">
            <div class="xmark-wrap">
                <svg viewBox="0 0 80 80">
                    <circle class="xmark-circle" cx="40" cy="40" r="38"/>
                    <line class="xmark-line" x1="28" y1="28" x2="52" y2="52"/>
                    <line class="xmark-line" x1="52" y1="28" x2="28" y2="52" style="animation-delay:0.55s"/>
                </svg>
            </div>
            <h1 style="color:#EF4444;">Verification Failed</h1>
            <div class="error-code">{safe_message}</div>
            <p>The link may have expired or already been used.</p>
            <a href="https://dashboard.cueapi.ai" class="btn-primary">Try Again &rarr;</a>
            <br>
            <a href="mailto:support@cueapi.ai" class="btn-secondary">Contact support</a>
        </div>"""
    return brand_page("CueAPI - Error", body)
