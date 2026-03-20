"""Tests for cron timezone handling — next_run must respect schedule_timezone."""
from __future__ import annotations

from datetime import datetime, timezone

import pytz
import pytest

from app.services.cue_service import get_next_run


@pytest.mark.asyncio
async def test_cron_respects_timezone_los_angeles():
    """Cron '0 9 * * *' with America/Los_Angeles should fire at 9am PT, not 9am UTC."""
    # Use a known UTC time: 2026-03-20 10:00 UTC = 2026-03-20 03:00 PT (PDT)
    after_utc = datetime(2026, 3, 20, 10, 0, 0, tzinfo=timezone.utc)

    next_run = get_next_run("0 9 * * *", "America/Los_Angeles", after=after_utc)

    # Next 9am PT is 2026-03-20 09:00 PDT = 2026-03-20 16:00 UTC
    assert next_run.tzinfo is not None, "next_run must be timezone-aware"
    assert next_run.year == 2026
    assert next_run.month == 3
    assert next_run.day == 20
    assert next_run.hour == 16  # 9am PDT = 16:00 UTC
    assert next_run.minute == 0

    # Must NOT be 9am UTC
    assert next_run.hour != 9, "Cron fired at 9am UTC instead of 9am PT — timezone ignored"


@pytest.mark.asyncio
async def test_cron_respects_timezone_utc():
    """Cron '0 9 * * *' with UTC should fire at 9am UTC."""
    after_utc = datetime(2026, 3, 20, 10, 0, 0, tzinfo=timezone.utc)

    next_run = get_next_run("0 9 * * *", "UTC", after=after_utc)

    # Next 9am UTC is 2026-03-21 09:00 UTC (since we're past 9am on the 20th)
    assert next_run.hour == 9
    assert next_run.day == 21


@pytest.mark.asyncio
async def test_cron_respects_timezone_india():
    """Cron '0 9 * * *' with Asia/Kolkata should fire at 9am IST (3:30 UTC)."""
    # Use a time that's after 9am IST: 2026-03-20 05:00 UTC = 2026-03-20 10:30 IST
    after_utc = datetime(2026, 3, 20, 5, 0, 0, tzinfo=timezone.utc)

    next_run = get_next_run("0 9 * * *", "Asia/Kolkata", after=after_utc)

    # Next 9am IST is 2026-03-21 09:00 IST = 2026-03-21 03:30 UTC
    assert next_run.tzinfo is not None
    assert next_run.day == 21
    assert next_run.hour == 3
    assert next_run.minute == 30


@pytest.mark.asyncio
async def test_cron_timezone_with_utc_after_param():
    """When 'after' is UTC (like from the poller), timezone must still be applied."""
    # Simulate poller passing a stored next_run in UTC
    # Previous fire was at 16:00 UTC (9am PDT) on March 20
    after_utc = datetime(2026, 3, 20, 16, 0, 0, tzinfo=timezone.utc)

    next_run = get_next_run("0 9 * * *", "America/Los_Angeles", after=after_utc)

    # Next 9am PDT should be March 21 at 16:00 UTC
    assert next_run.day == 21
    assert next_run.hour == 16  # 9am PDT = 16:00 UTC
    assert next_run.minute == 0


@pytest.mark.asyncio
async def test_cron_timezone_consecutive_fires():
    """Multiple consecutive next_run calculations should stay in the correct timezone."""
    # Start from a known point
    after = datetime(2026, 3, 19, 16, 0, 0, tzinfo=timezone.utc)  # 9am PDT March 19

    # Simulate 3 consecutive daily fires
    for expected_day in [20, 21, 22]:
        next_run = get_next_run("0 9 * * *", "America/Los_Angeles", after=after)
        assert next_run.day == expected_day, f"Expected day {expected_day}, got {next_run.day}"
        assert next_run.hour == 16, f"Expected 16:00 UTC (9am PDT), got {next_run.hour}:00 UTC"
        after = next_run  # Use this as the base for the next calculation
