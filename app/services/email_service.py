"""Centralized email service for CueAPI.

All emails go through this module. Test cue emails are suppressed.
Auth emails (magic link, key rotation) always send.
"""

from __future__ import annotations

import logging

from app.config import settings

logger = logging.getLogger(__name__)

TEST_CUE_PREFIXES = [
    "argus-",
    "test-",
    "verify-",
    "verify-production-",
    "verify-diag-",
    "e2e-",
    "test-cueapi-core-",
    "cueapi-core-test-",
]


def is_test_cue(cue_name: str) -> bool:
    """Returns True if this is a test/ephemeral cue that should not trigger emails."""
    if not cue_name:
        return False
    return any(cue_name.lower().startswith(prefix) for prefix in TEST_CUE_PREFIXES)


def send_email(to: str, subject: str, html: str, cue_name: str | None = None) -> bool:
    """Send an email via Resend.

    Args:
        to: Recipient email address.
        subject: Email subject line.
        html: Email body HTML.
        cue_name: If provided and is a test cue, email is suppressed.

    Returns:
        True if sent, False if suppressed or failed.
    """
    if cue_name and is_test_cue(cue_name):
        logger.info("Email suppressed for test cue: %s | subject: %s", cue_name, subject)
        return False

    if not settings.RESEND_API_KEY:
        logger.info("RESEND_API_KEY not set, skipping email: %s", subject)
        return False

    try:
        import resend

        resend.api_key = settings.RESEND_API_KEY
        from_email = getattr(settings, "RESEND_FROM_EMAIL", "CueAPI <alerts@cueapi.ai>")
        resend.Emails.send({
            "from": from_email,
            "to": to,
            "subject": subject,
            "html": html,
        })
        logger.info("Email sent | to: %s | subject: %s", to, subject)
        return True
    except ImportError:
        logger.info("resend not installed, skipping email: %s", subject)
        return False
    except Exception as e:
        logger.error("Email send failed | to: %s | subject: %s | error: %s", to, subject, e)
        return False
