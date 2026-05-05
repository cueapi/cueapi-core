"""Regression: ensure DispatchOutbox model agrees with its migration history.

Migration 002 created ``dispatch_outbox`` with:
  - ``id BIGSERIAL PRIMARY KEY``
  - ``execution_id UUID REFERENCES executions(id) ON DELETE CASCADE`` (NOT NULL)
  - ``cue_id VARCHAR(20)`` (NOT NULL, no FK declared in migration)

Migration 021 (messaging primitive) relaxed both ``execution_id`` and
``cue_id`` to NULLABLE to support message-task rows, extended the
``valid_task_type`` check, and added a ``task_payload_shape`` check.

The model historically declared ``id`` as plain ``Integer`` (32-bit) and
omitted the ``ForeignKey`` declarations on both ``execution_id`` and
``cue_id``. Tests use ``Base.metadata.create_all`` (NOT alembic
migrations) to spin up the schema, so a model that drifts from the
intended schema silently builds a slightly different table for the test
suite than what production runs.

This test asserts the model carries both FK declarations and a 64-bit
id. If someone reintroduces ``Integer`` or drops a FK, this test fails
before the drift can ship.

Ported from cueapi/cueapi#594.
"""
from __future__ import annotations

from sqlalchemy import BigInteger, Integer

from app.models.dispatch_outbox import DispatchOutbox


def test_id_is_bigint() -> None:
    id_col = DispatchOutbox.__table__.c.id
    assert isinstance(id_col.type, BigInteger), (
        f"DispatchOutbox.id must be BigInteger to match migration 002 "
        f"(BIGSERIAL); got {type(id_col.type).__name__}."
    )
    assert not (type(id_col.type) is Integer), (
        "DispatchOutbox.id is plain Integer (32-bit); migration 002 "
        "uses BIGSERIAL. Use BigInteger."
    )


def test_execution_id_has_cascade_fk() -> None:
    exec_col = DispatchOutbox.__table__.c.execution_id
    fks = list(exec_col.foreign_keys)
    assert len(fks) == 1, (
        f"DispatchOutbox.execution_id must have a FK to executions(id) "
        f"per migration 002; got {len(fks)} foreign keys."
    )
    fk = fks[0]
    assert fk.column.table.name == "executions"
    assert fk.column.name == "id"
    assert fk.ondelete == "CASCADE"


def test_cue_id_has_cascade_fk() -> None:
    """Model declares the FK to cues(id) ON DELETE CASCADE.

    The model originally omitted the FK declaration. The DB-level
    constraint is enforced via the cues→dispatch_outbox cascade chain
    when a cue is deleted (executions cascade-delete first, which then
    cascades dispatch_outbox via the execution FK). But the SQLAlchemy
    ORM didn't know about the direct FK, blocking any future
    ``relationship()`` traversal across the link.
    """
    cue_col = DispatchOutbox.__table__.c.cue_id
    fks = list(cue_col.foreign_keys)
    assert len(fks) == 1, (
        f"DispatchOutbox.cue_id must have a FK to cues(id); "
        f"got {len(fks)} foreign keys."
    )
    fk = fks[0]
    assert fk.column.table.name == "cues"
    assert fk.column.name == "id"
    assert fk.ondelete == "CASCADE"


def test_execution_id_and_cue_id_nullable_post_021() -> None:
    """Migration 021 relaxed both to NULLABLE for message-task rows."""
    assert DispatchOutbox.__table__.c.execution_id.nullable is True
    assert DispatchOutbox.__table__.c.cue_id.nullable is True
