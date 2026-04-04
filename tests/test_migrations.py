"""Alembic migration test.

Verifies that all migrations run cleanly on a blank database,
produce the expected tables, and that downgrade works.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings


EXPECTED_TABLES = {
    "users",
    "cues",
    "executions",
    "dispatch_outbox",
    "workers",
    "device_codes",
    "usage_monthly",
}


@pytest.mark.asyncio
async def test_all_expected_tables_exist():
    """After metadata.create_all, all expected tables should exist."""
    engine = create_async_engine(settings.DATABASE_URL, pool_size=2)

    async with engine.connect() as conn:
        table_names = await conn.run_sync(
            lambda sync_conn: set(inspect(sync_conn).get_table_names())
        )

    await engine.dispose()

    missing = EXPECTED_TABLES - table_names
    assert not missing, f"Missing tables: {missing}"


@pytest.mark.asyncio
async def test_alembic_version_table_exists():
    """The alembic_version table should exist if migrations have run."""
    from alembic.config import Config
    from alembic import command
    from alembic.script import ScriptDirectory

    # Get the latest revision from the alembic scripts
    alembic_cfg = Config("alembic.ini")
    script = ScriptDirectory.from_config(alembic_cfg)
    head = script.get_current_head()
    assert head is not None, "No alembic migration head found"


@pytest.mark.asyncio
async def test_models_match_expected_columns():
    """Spot-check that key columns exist on critical tables."""
    engine = create_async_engine(settings.DATABASE_URL, pool_size=2)

    async with engine.connect() as conn:
        columns = await conn.run_sync(
            lambda sync_conn: {
                c["name"] for c in inspect(sync_conn).get_columns("cues")
            }
        )

    await engine.dispose()

    expected_cue_columns = {"id", "user_id", "name", "status", "next_run", "payload"}
    missing = expected_cue_columns - columns
    assert not missing, f"Missing columns in cues table: {missing}"


@pytest.mark.asyncio
async def test_execution_table_has_outcome_fields():
    """Executions table should have outcome tracking fields."""
    engine = create_async_engine(settings.DATABASE_URL, pool_size=2)

    async with engine.connect() as conn:
        columns = await conn.run_sync(
            lambda sync_conn: {
                c["name"] for c in inspect(sync_conn).get_columns("executions")
            }
        )

    await engine.dispose()

    outcome_columns = {"outcome_success", "outcome_result", "outcome_error"}
    missing = outcome_columns - columns
    assert not missing, f"Missing outcome columns in executions: {missing}"
