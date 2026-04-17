"""Alert webhook delivery — HMAC-signed, SSRF-protected, best-effort.

This module ships the OSS alert-delivery path. Self-hosters configure
``alert_webhook_url`` on their user; alerts fire a signed POST. If no
URL is set, this is a no-op — alerts remain queryable via
``GET /v1/alerts``. Hosted cueapi.ai layers SendGrid on top; that
integration is not shipped in OSS.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

import httpx

from app.config import settings
from app.models.alert import Alert
from app.utils.signing import sign_payload
from app.utils.url_validation import validate_url_at_delivery

logger = logging.getLogger(__name__)

# Shorter than regular webhook delivery (30s) — alert delivery must
# not block outcome reporting. If a user's alert endpoint is slow, we
# give up and log rather than waiting.
ALERT_WEBHOOK_TIMEOUT_SECONDS = 10.0


def _alert_payload(alert: Alert) -> dict:
    return {
        "alert_id": str(alert.id),
        "alert_type": alert.alert_type,
        "severity": alert.severity,
        "message": alert.message,
        "execution_id": str(alert.execution_id) if alert.execution_id else None,
        "cue_id": alert.cue_id,
        "created_at": alert.created_at.isoformat() if alert.created_at else None,
        "metadata": alert.alert_metadata or {},
    }


async def deliver_alert(
    alert: Alert,
    alert_webhook_url: Optional[str],
    alert_webhook_secret: Optional[str],
) -> bool:
    """Fire an HMAC-signed POST to the user's alert webhook.

    Returns True on 2xx, False otherwise (including all errors). Never
    raises — alert delivery is best-effort and must not propagate into
    the outcome-reporting transaction.

    If ``alert_webhook_url`` is empty/None, returns False silently —
    the alert row is already persisted and queryable via
    ``GET /v1/alerts``.
    """
    if not alert_webhook_url:
        return False
    if not alert_webhook_secret:
        # A URL without a secret is a misconfiguration — log once,
        # don't deliver unsigned.
        logger.warning(
            "Alert webhook configured without signing secret; skipping delivery. user_id=%s alert_id=%s",
            alert.user_id, alert.id,
        )
        return False

    # SSRF: re-resolve at delivery time (DNS rebind protection).
    is_valid, ssrf_error = validate_url_at_delivery(alert_webhook_url, settings.ENV)
    if not is_valid:
        logger.warning(
            "Alert webhook SSRF-blocked at delivery: url=%s error=%s user_id=%s alert_id=%s",
            alert_webhook_url, ssrf_error, alert.user_id, alert.id,
        )
        return False

    payload = _alert_payload(alert)
    timestamp, signature = sign_payload(payload, alert_webhook_secret)
    headers = {
        "Content-Type": "application/json",
        "X-CueAPI-Signature": signature,
        "X-CueAPI-Timestamp": timestamp,
        "X-CueAPI-Alert-Id": str(alert.id),
        "X-CueAPI-Alert-Type": alert.alert_type,
        "User-Agent": "CueAPI/1.0",
    }

    try:
        async with httpx.AsyncClient(
            timeout=ALERT_WEBHOOK_TIMEOUT_SECONDS,
            follow_redirects=False,
        ) as client:
            # sort_keys=True matches sign_payload's serialization so
            # receivers can verify by recomputing over request.body().
            content = json.dumps(payload, sort_keys=True, default=str)
            response = await client.post(alert_webhook_url, headers=headers, content=content)
            if 200 <= response.status_code < 300:
                return True
            logger.warning(
                "Alert webhook non-2xx: status=%d url=%s alert_id=%s",
                response.status_code, alert_webhook_url, alert.id,
            )
            return False
    except httpx.TimeoutException:
        logger.warning("Alert webhook timed out: url=%s alert_id=%s", alert_webhook_url, alert.id)
        return False
    except httpx.ConnectError as e:
        logger.warning("Alert webhook connect error: url=%s alert_id=%s err=%s", alert_webhook_url, alert.id, e)
        return False
    except Exception as e:
        logger.warning("Alert webhook unexpected error: url=%s alert_id=%s err=%s", alert_webhook_url, alert.id, e)
        return False
