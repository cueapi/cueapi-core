"""Push delivery for the messaging primitive.

Spec: <https://trydock.ai/mike/cueapi-messaging-primitive-v1-sp> §5
(Push delivery).

Mirrors ``app/services/webhook.py:deliver_webhook`` (cue webhook
delivery) — same HMAC-SHA256-with-timestamp signing pattern, same
SSRF re-validation at delivery time, same httpx-with-no-redirects
client. Differences:

* Body shape per §5.3 (message-shaped, not cue-shaped).
* Headers carry Message/Agent/Thread ids instead of Cue/Execution ids.
* Always POST (no method-override; agents push, no GET-as-delivery).
* Reads ``to_agent.webhook_url`` and ``to_agent.webhook_secret`` live
  on each call so the caller is responsible for fetching the Agent
  fresh before each delivery attempt (supports rotation per §5.1).

Slice 3b (Phase 12.1.5) refactor: returns a ``DeliveryAttemptResult``
carrying the granular ``DeliveryClassification`` plus the raw
``Retry-After`` header value (when present). The caller (worker task)
uses the classification to decide retry-vs-terminal and the
``Retry-After`` value to set the next-attempt scheduled_at when
honoring 429/503 rate-limit signals.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from app.config import settings
from app.models import Agent, Message
from app.services.message_classification import (
    DeliveryClassification,
    classify_exception,
    classify_response,
    EVT_4XX_TERMINAL,
)
from app.utils.signing import sign_payload
from app.utils.url_validation import validate_url_at_delivery

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeliveryAttemptResult:
    """The outcome of a single push-delivery attempt.

    Carries everything the worker needs to decide retry-vs-terminal:

    * ``classification`` — granular taxonomy verdict (see
      ``app/services/message_classification.py``).
    * ``retry_after_header`` — raw ``Retry-After`` header value from
      the recipient (only set on 429 / 503 responses that include it;
      ``None`` otherwise).
    * ``response_body`` — recipient response body (truncated by httpx
      to a reasonable size). May be ``None`` when no response was
      received (transport-level failure).
    """

    classification: DeliveryClassification
    retry_after_header: Optional[str]
    response_body: Optional[str]


def _build_body(
    msg: Message,
    from_agent: Agent,
    to_agent: Agent,
    sender_user_slug: str,
    recipient_user_slug: str,
) -> dict:
    """Render the §5.3 POST body for a message delivery.

    Slug-form addresses are computed from the live agent + user slugs
    rather than stored on the message — supports the slug-mutation
    edge case (renamed agent) without dispatch-payload churn.
    """
    return {
        "id": msg.id,
        "from": {
            "agent_id": from_agent.id,
            "slug": f"{from_agent.slug}@{sender_user_slug}",
        },
        "to": {
            "agent_id": to_agent.id,
            "slug": f"{to_agent.slug}@{recipient_user_slug}",
        },
        "thread_id": msg.thread_id,
        "reply_to": msg.reply_to,
        "subject": msg.subject,
        "body": msg.body,
        "priority": msg.priority,
        "expects_reply": msg.expects_reply,
        "reply_to_agent_id": msg.reply_to_agent_id,
        "metadata": msg.metadata_ or {},
        "created_at": msg.created_at.isoformat() if msg.created_at else None,
    }


async def deliver_message_to_webhook(
    *,
    msg: Message,
    from_agent: Agent,
    to_agent: Agent,
    sender_user_slug: str,
    recipient_user_slug: str,
    attempt: int,
    timeout: int = 0,
) -> DeliveryAttemptResult:
    """POST a message to ``to_agent.webhook_url`` with HMAC-SHA256
    signed headers, classify the outcome, and return a
    ``DeliveryAttemptResult``.

    The caller MUST have fetched ``to_agent`` live (not from the
    outbox payload) so this function sees the current
    ``webhook_url`` + ``webhook_secret`` per §5.1 (rotation safety).
    The caller MUST have already verified ``to_agent.webhook_url is
    not None``; this function does NOT no-op when the URL is missing.
    """
    if not to_agent.webhook_url or not to_agent.webhook_secret:
        # Defensive — caller is supposed to skip this case. Treat as
        # a terminal client_error so the caller marks failed without
        # retrying (recipient explicitly opted out of push between
        # outbox enqueue and worker pickup; the no-op-and-leave-queued
        # path is handled at a higher level in deliver_message_task).
        return DeliveryAttemptResult(
            classification=DeliveryClassification(
                category="terminal",
                error_type="missing_webhook_config",
                log_event_type=EVT_4XX_TERMINAL,
                error_message="to_agent has no webhook_url/webhook_secret",
                http_status=None,
            ),
            retry_after_header=None,
            response_body=None,
        )

    body = _build_body(
        msg=msg,
        from_agent=from_agent,
        to_agent=to_agent,
        sender_user_slug=sender_user_slug,
        recipient_user_slug=recipient_user_slug,
    )
    timestamp, signature = sign_payload(body, to_agent.webhook_secret)

    # ``X-CueAPI-Event-Type`` is a Slice-2 add (Max's recipient-side
    # review): future-proofs for v1.5 state-transition webhooks
    # (``message.delivered`` / ``message.read`` / ``message.acked``).
    # Without it, recipients infer "new message" from context; with
    # it, handlers switch on event type cleanly. v1 only emits
    # ``message.created``.
    headers = {
        "Content-Type": "application/json",
        "X-CueAPI-Signature": signature,
        "X-CueAPI-Timestamp": timestamp,
        "X-CueAPI-Event-Type": "message.created",
        "X-CueAPI-Message-Id": msg.id,
        "X-CueAPI-Agent-Id": to_agent.id,
        "X-CueAPI-Thread-Id": msg.thread_id,
        "X-CueAPI-Attempt": str(attempt),
        "User-Agent": "CueAPI/1.0",
    }

    # SSRF: re-validate the URL at delivery time per §5.5 (DNS rebind
    # protection — the URL was validated at register-time, but DNS may
    # have changed between then and now).
    is_valid, ssrf_error = validate_url_at_delivery(
        to_agent.webhook_url, settings.ENV
    )
    if not is_valid:
        logger.warning(
            "SSRF blocked at delivery: url=%s, error=%s, message_id=%s",
            to_agent.webhook_url,
            ssrf_error,
            msg.id,
        )
        return DeliveryAttemptResult(
            classification=DeliveryClassification(
                category="terminal",
                error_type="ssrf_blocked",
                log_event_type=EVT_4XX_TERMINAL,
                error_message=f"SSRF blocked at delivery: {ssrf_error}",
                http_status=None,
            ),
            retry_after_header=None,
            response_body=None,
        )

    body_bytes = json.dumps(body, sort_keys=True, default=str).encode("utf-8")

    # Note: a fresh httpx.AsyncClient is created per attempt. This is
    # intentional — it ensures no DNS caching across retries, which
    # matters for recipients with dynamic IPs (Tailscale node
    # re-registration, dynamic DNS). Documented in spec §5.4.
    try:
        async with httpx.AsyncClient(
            timeout=timeout or settings.WEBHOOK_TIMEOUT_SECONDS,
            follow_redirects=False,
        ) as client:
            response = await client.post(
                to_agent.webhook_url,
                content=body_bytes,
                headers=headers,
            )
        # Capture Retry-After header for 429 / 503 responses; ignored
        # otherwise. Pass through raw value; ``parse_retry_after``
        # handles the ``max(own_min, retry_after)`` semantics.
        retry_after = None
        if response.status_code in (429, 503):
            retry_after = response.headers.get("Retry-After")
        return DeliveryAttemptResult(
            classification=classify_response(response.status_code),
            retry_after_header=retry_after,
            response_body=response.text,
        )
    except httpx.RequestError as e:
        logger.info(
            "Push delivery transport error: message_id=%s, url=%s, type=%s, error=%s",
            msg.id, to_agent.webhook_url, type(e).__name__, e,
        )
        return DeliveryAttemptResult(
            classification=classify_exception(e),
            retry_after_header=None,
            response_body=None,
        )
