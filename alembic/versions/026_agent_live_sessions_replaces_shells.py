"""Replace agent_shells with agent_live_sessions (commit 2 of 5).

**Breaking change.** Drops the `agent_shells` table introduced in
migration 023 and replaces it with `agent_live_sessions`, ported from
private cueapi (its migrations 053 + 054 collapsed into a single OSS
migration). The new schema carries the columns required for cmotigtnx
Live-attestation (`session_token`) and the per-session presence model
that the cueapi-presence-runtime package will consume.

Why hard-cut (announced in PR #61, CHANGELOG / parity-manifest):

* OSS is currently pre-1.0 (v0.2.x); breaking changes are explicitly
  permitted.
* No production consumer of `agent_shells` exists today —
  cue-mac-app's wire-through commits never pushed; Dock's daemon
  polls `/v1/agents/{ref}/inbox` rather than `/shells/*`.
* Maintaining `agent_shells` as a deprecated alias would create
  double-maintenance and a stale-sync surface for zero benefit.

Why webhook_url / webhook_secret are NOT preserved:

Per substrate review CONCUR (cueapi-secondary, 2026-05-09): YAGNI +
substrate-stays-narrow doctrine. Per-session webhook fan-out delivery
is not implemented today; pre-adding columns creates implicit API
surface that is costly to change later. If/when fan-out delivery
becomes a real ask, both columns can be added back additively in a
follow-up migration ("additive-later is trivial").

Migration safety (DDL-in-transaction):

OSS does not run rolling deploys (single-instance Railway pattern).
Postgres supports DDL inside a transaction, so the `DROP TABLE` →
`CREATE TABLE` sequence within one alembic head is observably atomic
to readers. The brief between-state is not visible.

Index strategy:

* `ix_agent_live_sessions_cue_id` (unique) — cue_id is globally
  unique across agents; reusing one across agents is a wire-format
  error.
* `ix_agent_live_sessions_active` (partial, `WHERE detached_at IS NULL`)
  — directory-render hot path.
* `ux_agent_live_sessions_one_default_per_agent` (partial unique,
  `WHERE is_default = true AND detached_at IS NULL`) — at most one
  default-routing session per agent.
* `ux_agent_live_sessions_label_per_agent` (partial unique,
  `WHERE detached_at IS NULL`) — labels unique per agent among
  active sessions; re-attach with same label after detach is allowed
  (new row, old row stays in audit trail).

Naming convention is `ix_*` / `ux_*` per private cueapi (verbatim
parity wins for cross-codebase diff cleanliness; deviates from
migration 024's `idx_*` prefix used in the counterpart-port — that
was the in-OSS convention at the time, but agent_live_sessions ports
private's source of truth so private's naming wins). Documented
explicitly here so future maintainers see the deviation as
intentional, not drift.

CONCURRENTLY hygiene:

The new table is empty at migration time, so `CREATE INDEX
CONCURRENTLY` is not strictly required. But matching the in-flight
pattern from migration 024 (counterpart-port indexes on the
populated `messages` table) is a defensive hygiene default — if
someone retro-applies this migration on a populated test DB, or if
session-replay backfill ever needs to re-create indexes, the
CONCURRENTLY path is safe by construction.

`autocommit_block` is required because Postgres rejects
`CREATE INDEX CONCURRENTLY` inside a transaction. Same pattern as
migration 024.

Replacement endpoint surface (lands in commits 4 + 5):

* `POST /v1/agents/{ref}/live-sessions` (register / re-attach)
* `GET /v1/agents/{ref}/live-sessions` (list)
* `PATCH /v1/agents/{ref}/live-sessions/{id}` (relabel, set default)
* `DELETE /v1/agents/{ref}/live-sessions/{id}` (soft detach)
* `POST /v1/agents/{ref}/live-sessions/{id}/heartbeat`
* `POST /v1/executions/{id}/live-claim` (cmotigtnx attestation)

Endpoints under `/v1/agents/{ref}/shells/*` are removed in commit 4.

Tracking:

* Design note: https://trydock.ai/workspaces/cueapi-agent-live-sessions-port-2026-05-09
* Substrate review: cueapi-secondary CONCUR 2026-05-09 (lock-vote SHIP)
* Announcement (commit 1): PR #61 — parity-manifest + CHANGELOG entry
* This migration (commit 2): PR # (current)
* Model + service (commit 3): pending
* Endpoints (commit 4): pending
* Tests + multi-session.md doc (commit 5): pending

Revision ID: 026
Revises: 025
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID


revision = "026"
down_revision = "025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ─── Drop agent_shells (introduced in 023) ─────────────────────
    #
    # Hard-cut deprecation. The table is replaced by agent_live_sessions
    # below. Index drop comes first to avoid any orphan-index races.
    op.drop_index("ix_agent_shells_active", table_name="agent_shells")
    op.drop_table("agent_shells")

    # ─── Create agent_live_sessions (replaces agent_shells) ────────
    #
    # ``agents.id`` is ``String(20)`` (the ``agt_<12 alphanum>`` opaque-ID
    # format from ``app.utils.ids.generate_agent_id``), NOT a UUID. The FK
    # column ``agent_id`` mirrors that type so Postgres can implement the
    # FK. Live-session ``id`` itself stays UUID — internal opaque, no
    # external addressing requirement.
    op.create_table(
        "agent_live_sessions",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "agent_id",
            sa.String(20),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("label", sa.Text, nullable=False),
        sa.Column("cue_id", sa.Text, nullable=False),
        sa.Column("task_name", sa.Text, nullable=False),
        sa.Column(
            "is_default",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        # Hot-path presence signals — heartbeat ticks every ~60s,
        # last_claim_at bumps on every successful Live claim. Both
        # nullable (NULL = never observed yet).
        sa.Column("attached_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detached_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_claim_at", sa.DateTime(timezone=True), nullable=True),
        # Optional client metadata so the directory UI can surface
        # mismatches (e.g. one session on v2.1, another on v1).
        # Format convention: semver-style string ("v2.1.0") preferred
        # for sortable string compare; commit-SHA fallback acceptable
        # but consumers should compare with caution if mixed.
        sa.Column("monitor_version", sa.Text, nullable=True),
        # cmotigtnx attestation column (see private migration 054).
        # Crockford-base32 ULID written by the Monitor at attach time
        # and on Monitor restart. The /v1/executions/{id}/live-claim
        # endpoint cross-references the POSTed ULID against this
        # column for the matching label/task_name.
        sa.Column("session_token", sa.String(80), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # ─── Indexes (CONCURRENTLY for defensive hygiene) ──────────────
    #
    # Postgres requires CONCURRENTLY outside a transaction. autocommit_block
    # opens a separate connection in autocommit mode, same pattern as
    # migration 024_messaging_counterpart_indexes.py.
    with op.get_context().autocommit_block():
        # cue_id is globally unique across all agents — a session
        # reusing a cue_id across agents would mean two agents claim
        # the same routing target.
        op.create_index(
            "ix_agent_live_sessions_cue_id",
            "agent_live_sessions",
            ["cue_id"],
            unique=True,
            postgresql_concurrently=True,
            if_not_exists=True,
        )

        # Active-session lookup hot path for directory render. Partial
        # index keeps the scan small as the audit trail accumulates.
        op.create_index(
            "ix_agent_live_sessions_active",
            "agent_live_sessions",
            ["agent_id", "last_heartbeat"],
            postgresql_where=sa.text("detached_at IS NULL"),
            postgresql_concurrently=True,
            if_not_exists=True,
        )

        # Partial unique: at most one is_default=true active session
        # per agent. App layer flips use single-statement UPDATE so
        # the swap is atomic against this constraint (no
        # zero-or-two-defaults window).
        op.create_index(
            "ux_agent_live_sessions_one_default_per_agent",
            "agent_live_sessions",
            ["agent_id"],
            unique=True,
            postgresql_where=sa.text("is_default = true AND detached_at IS NULL"),
            postgresql_concurrently=True,
            if_not_exists=True,
        )

        # Partial unique: label is unique per agent among active
        # sessions. Re-attaching with the same label after detach
        # is allowed — new row, old row stays in audit trail with
        # detached_at set.
        op.create_index(
            "ux_agent_live_sessions_label_per_agent",
            "agent_live_sessions",
            ["agent_id", "label"],
            unique=True,
            postgresql_where=sa.text("detached_at IS NULL"),
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    # ─── Drop agent_live_sessions ──────────────────────────────────
    with op.get_context().autocommit_block():
        op.drop_index(
            "ux_agent_live_sessions_label_per_agent",
            table_name="agent_live_sessions",
            postgresql_concurrently=True,
            if_exists=True,
        )
        op.drop_index(
            "ux_agent_live_sessions_one_default_per_agent",
            table_name="agent_live_sessions",
            postgresql_concurrently=True,
            if_exists=True,
        )
        op.drop_index(
            "ix_agent_live_sessions_active",
            table_name="agent_live_sessions",
            postgresql_concurrently=True,
            if_exists=True,
        )
        op.drop_index(
            "ix_agent_live_sessions_cue_id",
            table_name="agent_live_sessions",
            postgresql_concurrently=True,
            if_exists=True,
        )

    op.drop_table("agent_live_sessions")

    # ─── Re-create agent_shells (revert state) ─────────────────────
    #
    # Mirrors migration 023's upgrade path exactly. Allows a clean
    # downgrade for development / testing scenarios. Note that any
    # rows that lived in the original agent_shells table at
    # upgrade time were dropped — downgrade restores the schema,
    # not the data.
    op.create_table(
        "agent_shells",
        sa.Column("id", sa.String(length=20), primary_key=True),
        sa.Column(
            "agent_id",
            sa.String(length=20),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("webhook_url", sa.Text(), nullable=True),
        sa.Column("webhook_secret", sa.String(length=80), nullable=True),
        sa.Column("label", sa.String(length=128), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'online'"),
        ),
        sa.Column(
            "last_heartbeat_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "registered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('online', 'offline', 'away')",
            name="valid_shell_status",
        ),
        sa.CheckConstraint(
            "(webhook_url IS NULL) = (webhook_secret IS NULL)",
            name="shell_webhook_url_secret_paired",
        ),
    )

    op.create_index(
        "ix_agent_shells_active",
        "agent_shells",
        ["agent_id", "status", "last_heartbeat_at"],
    )
