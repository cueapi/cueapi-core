from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional, Tuple

import httpx

from app.config import settings
from app.utils.signing import sign_payload
from app.utils.url_validation import validate_url_at_delivery

logger = logging.getLogger(__name__)


async def deliver_webhook(
    callback_url: str,
    callback_method: str,
    callback_headers: dict,
    payload: dict,
    cue_id: str,
    cue_name: str,
    execution_id: str,
    scheduled_for: datetime,
    attempt: int,
    webhook_secret: str = "",
) -> Tuple[bool, Optional[int], Optional[str]]:
    """
    Deliver a webhook. Returns (success, http_status, error_or_response_body).

    webhook_secret: The user's per-user webhook signing secret.
    """
    webhook_body = {
        "execution_id": str(execution_id),
        "cue_id": cue_id,
        "name": cue_name,
        "scheduled_for": scheduled_for.isoformat(),
        "attempt": attempt,
        "payload": payload,
    }

    timestamp, signature = sign_payload(webhook_body, webhook_secret)

    headers = {
        "Content-Type": "application/json",
        "X-CueAPI-Signature": signature,
        "X-CueAPI-Timestamp": timestamp,
        "X-CueAPI-Cue-Id": cue_id,
        "X-CueAPI-Execution-Id": str(execution_id),
        "X-CueAPI-Scheduled-For": scheduled_for.isoformat(),
        "X-CueAPI-Attempt": str(attempt),
        "User-Agent": "CueAPI/1.0",
    }
    # Merge custom callback headers (they override defaults except signature headers)
    if callback_headers:
        headers.update(callback_headers)

    # SSRF: Re-validate URL at delivery time (DNS rebind protection)
    is_valid, ssrf_error = validate_url_at_delivery(callback_url, settings.ENV)
    if not is_valid:
        logger.warning(
            "SSRF blocked at delivery: url=%s, error=%s, cue_id=%s",
            callback_url, ssrf_error, cue_id,
        )
        return False, None, ssrf_error

    try:
        async with httpx.AsyncClient(
            timeout=settings.WEBHOOK_TIMEOUT_SECONDS,
            follow_redirects=False,
        ) as client:
            if callback_method.upper() == "GET":
                response = await client.get(callback_url, headers=headers)
            elif callback_method.upper() == "PUT":
                response = await client.put(callback_url, headers=headers, content=json.dumps(webhook_body, sort_keys=True, default=str))
            elif callback_method.upper() == "PATCH":
                response = await client.patch(callback_url, headers=headers, content=json.dumps(webhook_body, sort_keys=True, default=str))
            else:  # POST
                response = await client.post(callback_url, headers=headers, content=json.dumps(webhook_body, sort_keys=True, default=str))

            response_text = response.text[:2000]  # Limit stored response
            if 200 <= response.status_code < 300:
                return True, response.status_code, response_text
            else:
                error_msg = _meaningful_error(response.status_code)
                return False, response.status_code, error_msg

    except httpx.TimeoutException:
        return False, None, "Webhook endpoint timed out"
    except httpx.ConnectError:
        return False, None, "Could not connect to webhook endpoint"
    except Exception as e:
        logger.exception("Unexpected error delivering webhook")
        return False, None, str(e)[:500]


def _meaningful_error(status_code: int) -> str:
    """Convert HTTP status codes to human-readable error messages."""
    if status_code == 404:
        return "Webhook endpoint not found (404)"
    elif status_code == 408:
        return "Webhook endpoint timed out"
    elif status_code == 500:
        return "Webhook endpoint returned server error (500)"
    elif 400 <= status_code < 500:
        return f"Webhook endpoint rejected request ({status_code})"
    elif 500 <= status_code < 600:
        return f"Webhook endpoint server error ({status_code})"
    else:
        return f"Webhook delivery failed ({status_code})"
