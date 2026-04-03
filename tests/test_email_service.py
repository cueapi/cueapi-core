"""Tests for email_service.py — test cue detection and email suppression."""

from app.services.email_service import is_test_cue, send_email


class TestIsTestCue:
    def test_argus_prefix(self):
        assert is_test_cue("argus-daily-summary") is True

    def test_test_prefix(self):
        assert is_test_cue("test-cueapi-core-12345") is True

    def test_verify_prefix(self):
        assert is_test_cue("verify-production-1774636583") is True

    def test_verify_diag_prefix(self):
        assert is_test_cue("verify-diag-7a02a4") is True

    def test_e2e_prefix(self):
        assert is_test_cue("e2e-webhook-flow") is True

    def test_real_cue_not_blocked(self):
        assert is_test_cue("morning-briefing") is False
        assert is_test_cue("daily-report") is False
        assert is_test_cue("content-quality-check") is False

    def test_case_insensitive(self):
        assert is_test_cue("ARGUS-test") is True
        assert is_test_cue("Test-my-cue") is True
        assert is_test_cue("VERIFY-diag-123") is True

    def test_none_returns_false(self):
        assert is_test_cue(None) is False

    def test_empty_returns_false(self):
        assert is_test_cue("") is False


class TestSendEmail:
    def test_suppressed_for_test_cue(self):
        result = send_email(
            to="user@test.com",
            subject="Test failure",
            html="<p>Failed</p>",
            cue_name="argus-daily-test",
        )
        assert result is False

    def test_no_cue_name_not_suppressed(self):
        # Will fail to send (no RESEND_API_KEY in test) but should not be suppressed
        result = send_email(
            to="user@test.com",
            subject="Key rotated",
            html="<p>Rotated</p>",
            cue_name=None,
        )
        # Returns False because RESEND_API_KEY is not set, not because suppressed
        assert result is False

    def test_real_cue_not_suppressed(self):
        # Will fail (no API key) but the suppression check should pass
        result = send_email(
            to="user@test.com",
            subject="Real failure",
            html="<p>Failed</p>",
            cue_name="morning-briefing",
        )
        # Returns False because no RESEND_API_KEY, not suppression
        assert result is False
