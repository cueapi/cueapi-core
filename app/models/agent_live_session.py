"""AgentLiveSession — per-label Live attachment for an Agent.

Replaces ``AgentShell`` (the multi-shell-per-agent webhook-target
model from PR-5a) with a unified per-session table whose rows
represent attached Live sessions rather than webhook delivery targets.
Each row is one attached Live session for an agent; the row marked
``is_default=true`` is the routing target for senders who fire
without specifying a label.

Schema mirrors the private cueapi source — same column names, same
types, same partial-unique semantics. Migration
``026_agent_live_sessions_replaces_shells.py`` creates the table
plus four indexes:

* ``ix_agent_live_sessions_cue_id`` (unique) — cue_id is globally
  unique across agents; reusing one would mean two agents claim the
  same routing target.
* ``ix_agent_live_sessions_active`` — active-session lookup
  (``WHERE detached_at IS NULL``) for the directory-render hot path.
* ``ux_agent_live_sessions_one_default_per_agent`` (partial unique) —
  DB-enforced "at most one is_default=true active session per agent".
  App-layer flips use a single-statement UPDATE so the swap is atomic
  against this constraint (no zero-or-two-defaults window).
* ``ux_agent_live_sessions_label_per_agent`` (partial unique) —
  labels are unique per agent among active sessions. Re-attaching
  with the same label after detach is allowed (new row; old row
  stays in audit trail with ``detached_at`` set).

Detach is soft (sets ``detached_at = now()``) so claim-history can
be reconstructed for future polish work (``claim_success_rate_24h``
per session). Active-session indexes use
``WHERE detached_at IS NULL`` to skip historical rows without
bloating the scan.

``monitor_version`` format convention: semver-style strings
(``"v2.1.0"``) preferred for sortable string compare. Commit-SHA
fallback acceptable but consumers should compare with caution if the
two formats are mixed within a single deployment.

Backward compat: this REPLACES ``AgentShell`` entirely. The
``agent_shells`` table is dropped in migration 026. No alias
preserved — pre-1.0 OSS, no production consumers.
"""
from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class AgentLiveSession(Base):
    __tablename__ = "agent_live_sessions"
    # Indexes mirror migration 026's create_index calls. Declared on
    # the model so ``Base.metadata.create_all`` (test setup path) builds
    # them — the migration alone isn't enough for tests, since pytest
    # uses create_all not ``alembic upgrade head``.
    __table_args__ = (
        # cue_id is globally unique across agents; reusing one across
        # agents is a wire-format error (the cue routing target should
        # only point at one live session).
        Index("ix_agent_live_sessions_cue_id", "cue_id", unique=True),
        # Active-session lookup hot path for the directory render.
        Index(
            "ix_agent_live_sessions_active",
            "agent_id",
            "last_heartbeat",
            postgresql_where=text("detached_at IS NULL"),
        ),
        # At most one is_default=true active session per agent. App
        # layer flips use single-statement UPDATE so the swap is
        # atomic against this index.
        Index(
            "ux_agent_live_sessions_one_default_per_agent",
            "agent_id",
            unique=True,
            postgresql_where=text("is_default = true AND detached_at IS NULL"),
        ),
        # Labels are unique per agent among active sessions.
        # Re-attaching with the same label after detach is allowed
        # (new row, old row stays in audit trail with detached_at set).
        Index(
            "ux_agent_live_sessions_label_per_agent",
            "agent_id",
            "label",
            unique=True,
            postgresql_where=text("detached_at IS NULL"),
        ),
        # Item B Phase 1 indexes (migration 035): support per-daemon
        # reconcile + daily transport='poll' cleanup queries.
        Index("ix_agent_live_sessions_daemon", "daemon_id"),
        Index(
            "ix_agent_live_sessions_transport",
            "transport",
            "last_reconciled_at",
        ),
        # Item B Phase 1 CHECK constraint (migration 035): transport
        # values restricted to 'ipc' or 'poll'. VARCHAR+CHECK is the
        # CueAPI convention.
        CheckConstraint(
            "transport IN ('ipc', 'poll')",
            name="valid_transport",
        ),
    )

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    # ``agents.id`` is ``String(20)`` (the ``agt_<12 alphanum>`` opaque
    # ID format from ``app.utils.ids.generate_agent_id``), NOT UUID.
    # The FK column type must match the parent column for Postgres to
    # implement the FK. Live-session ``id`` above stays UUID (internal
    # opaque, no external addressing requirement).
    agent_id = Column(
        String(20),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
    )
    label = Column(Text, nullable=False)
    cue_id = Column(Text, nullable=False)
    task_name = Column(Text, nullable=False)
    is_default = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )

    # Hot-path presence signals — heartbeat ticks every ~60s,
    # last_claim_at bumps on every successful Live claim. Both
    # nullable (NULL = never observed yet).
    attached_at = Column(DateTime(timezone=True), nullable=True)
    detached_at = Column(DateTime(timezone=True), nullable=True)
    last_heartbeat = Column(DateTime(timezone=True), nullable=True)
    last_claim_at = Column(DateTime(timezone=True), nullable=True)

    # Optional client metadata so the directory UI can surface
    # mismatches (e.g. one session on v2.1.0, another on v1.x).
    # Format convention: semver-style strings preferred for sortable
    # compare; commit-SHA fallback acceptable but mixed-format
    # comparisons require caution.
    monitor_version = Column(Text, nullable=True)

    # cmotigtnx attestation column (ported from private migration 054).
    # Crockford-base32 ULID written by the Monitor at attach time and
    # on Monitor restart. The ``POST /v1/executions/{id}/live-claim``
    # endpoint cross-references the POSTed ULID against this column
    # for the matching label/task_name. Nullable: pre-Q-E
    # registrations have NULL; the live-claim validator's phase-1
    # grace accepts bare task_name on existing rows.
    session_token = Column(String(80), nullable=True)

    # Item B Phase 1 columns (migration 035, live-delivery-v3).
    # NB: existing v2.x rows inherit transport='poll' + NULL on others
    # — fire-accept dispatcher unchanged unless transport='ipc' is set.
    ipc_session_token = Column(String(32), nullable=True)
    transport = Column(
        String(8),
        nullable=False,
        server_default=text("'poll'"),
        default="poll",
    )
    daemon_id = Column(UUID(as_uuid=True), nullable=True)
    last_reconciled_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
