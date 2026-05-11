"""Event-emit primitive — events + subscriptions tables (PR-1b).

Ships the substrate for the Cue Messages design lock (Option D,
Mike 2026-05-10): a separate subscription-event delivery surface
that messaging service (PR-2a) will emit on. This migration lands
DORMANT — tables exist, endpoints respond, but no caller writes
``emit_event`` until PR-2a. Zero production behavior change at
merge.

Two tables:

* ``events`` — append-only event log. BIGSERIAL ``id`` is the
  monotonic cursor for pull pagination (92-year ceiling at 10^8
  events/year peak rate; documented inline below). UUID would
  add 8 bytes/row + need sortable encoding for no v0.1 benefit.
* ``subscriptions`` — agent intent. One active row per
  (agent, event_type, delivery_target) tuple; pull + webhook
  subs can coexist for the same event_type.

Indexes — all CONCURRENTLY per the alembic-collision-guard
pattern (PR #69). autocommit_block lifts each statement out of
the surrounding migration transaction so Postgres accepts
CONCURRENTLY.

CTO-locked decisions baked into the schema (2026-05-11):

* ``events.id`` BIGSERIAL (CTO Q2): monotonic cursor; revisit
  if multi-region replication enters roadmap.
* Subscription auth (CTO correction #1): no caller-supplied
  ``subscriber_agent_id`` override field; route-level resolves
  ``{ref}`` to an agent row whose ``user_id`` matches the
  authenticated user, then stamps that id.
* ``subscriptions.last_dispatched_event_id`` watermark (CTO
  correction #2): exposed via GET endpoint so recipients can
  observe paused-webhook state.
* Cleanup retention (CTO Q5): events older than 7 days are
  swept by a 1h cron arq task (separate scope).

Schema cross-checks against existing tables:

* ``agents.id`` is ``String(20)`` (``agt_xxx`` opaque ID per
  ``app/utils/ids.generate_agent_id``). FK columns here must
  match parent type — caught the same way ``agent_live_sessions``
  caught it in PR #672. Both ``events.recipient_agent_id`` and
  ``subscriptions.subscriber_agent_id`` use ``String(20)``.

Revision ID: 028
Revises: 027
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "028"
down_revision = "027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ───────────────────────────────────────────────────────────────
    # events table — append-only log; BIGSERIAL id is the cursor.
    # ───────────────────────────────────────────────────────────────
    op.create_table(
        "events",
        # BIGSERIAL for monotonic cursor ordering.
        # 92-year ceiling at peak 10^8 events/year (2^63 / 10^8 / 86400 / 365).
        # Plenty of headroom; revisit only if cross-region replication
        # demands ULID-style globally-unique ids.
        sa.Column(
            "id",
            sa.BigInteger,
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column(
            "recipient_agent_id",
            sa.String(20),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "emitted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("idempotency_key", sa.Text, nullable=True),
    )

    # ───────────────────────────────────────────────────────────────
    # subscriptions table — agent intent to consume an event type.
    # ───────────────────────────────────────────────────────────────
    op.create_table(
        "subscriptions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.func.gen_random_uuid(),
        ),
        sa.Column(
            "subscriber_agent_id",
            sa.String(20),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.Text, nullable=False),
        # 'pull' (default, universal floor) or 'webhook' (latency
        # optimization, opt-in via webhook_url at create-time).
        sa.Column("delivery_target", sa.Text, nullable=False),
        sa.Column("webhook_url", sa.Text, nullable=True),
        # Server-minted HMAC signing key for webhook subscriptions.
        # ``whsec_`` + 64 hex chars (matches per-user webhook_secret format).
        sa.Column("webhook_secret", sa.Text, nullable=True),
        # Watermark for resume + dispatch-due index. NULL = never
        # dispatched yet (first dispatch picks up all events).
        sa.Column(
            "last_dispatched_event_id",
            sa.BigInteger,
            nullable=True,
        ),
        sa.Column(
            "last_dispatched_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        # Circuit-breaker counter; reset to 0 on successful dispatch.
        sa.Column(
            "consecutive_failures",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
        # Circuit-breaker pause window. NULL or past = active;
        # future = paused until that time.
        sa.Column(
            "paused_until",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # Soft-delete for idempotent unsubscribe + audit trail.
        sa.Column(
            "detached_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.CheckConstraint(
            "delivery_target IN ('pull', 'webhook')",
            name="subscriptions_delivery_target_check",
        ),
        sa.CheckConstraint(
            "(delivery_target = 'pull' AND webhook_url IS NULL) OR "
            "(delivery_target = 'webhook' AND webhook_url IS NOT NULL)",
            name="subscriptions_webhook_url_check",
        ),
    )

    # ───────────────────────────────────────────────────────────────
    # Indexes — CONCURRENTLY required for the events table because
    # it will be the hottest write path once PR-2a wires emission.
    # autocommit_block lifts each statement out of the migration
    # transaction so Postgres accepts CONCURRENTLY.
    # ───────────────────────────────────────────────────────────────
    with op.get_context().autocommit_block():
        # events: pull cursor query — WHERE recipient_agent_id = $1
        # AND id > $cursor ORDER BY id ASC LIMIT N.
        op.create_index(
            "ix_events_recipient_id_cursor",
            "events",
            ["recipient_agent_id", "id"],
            postgresql_concurrently=True,
            if_not_exists=True,
        )

        # events: emit idempotency. Re-emitting the same
        # (event_type, idempotency_key) silently returns the existing
        # event_id (matches dispatch_outbox pattern).
        op.create_index(
            "ux_events_idempotency_key",
            "events",
            ["event_type", "idempotency_key"],
            unique=True,
            postgresql_where=sa.text("idempotency_key IS NOT NULL"),
            postgresql_concurrently=True,
            if_not_exists=True,
        )

        # events: cleanup job target. Full index on emitted_at — a
        # partial-predicate index using NOW() would be ideal but
        # Postgres rejects it (functions in index predicates must be
        # IMMUTABLE, and NOW() is STABLE). The cleanup query's range
        # scan over this index is fast enough; the 7-day cutoff
        # filter happens at query-execution time, not in the index.
        op.create_index(
            "ix_events_emitted_at",
            "events",
            ["emitted_at"],
            postgresql_concurrently=True,
            if_not_exists=True,
        )

        # subscriptions: at most one active subscription per
        # (agent, event_type, delivery_target) tuple. An agent CAN
        # have both pull + webhook subs for the same event_type
        # (concurrent dual-surface — defensive doubling, e.g.,
        # during webhook outage).
        op.create_index(
            "ux_subscriptions_active_unique",
            "subscriptions",
            ["subscriber_agent_id", "event_type", "delivery_target"],
            unique=True,
            postgresql_where=sa.text("detached_at IS NULL"),
            postgresql_concurrently=True,
            if_not_exists=True,
        )

        # subscriptions: dispatch-due lookup for the webhook dispatch
        # loop. Reads this index to find webhook subs with pending
        # events (events.id > last_dispatched_event_id). The
        # ``paused_until`` filter happens at query time — partial
        # predicates can't reference NOW() (must be IMMUTABLE). The
        # index still excludes detached + non-webhook subs, which is
        # the bulk of the selectivity.
        op.create_index(
            "ix_subscriptions_dispatch_due",
            "subscriptions",
            ["last_dispatched_event_id"],
            postgresql_where=sa.text(
                "delivery_target = 'webhook' AND detached_at IS NULL"
            ),
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_subscriptions_dispatch_due",
            table_name="subscriptions",
            postgresql_concurrently=True,
            if_exists=True,
        )
        op.drop_index(
            "ux_subscriptions_active_unique",
            table_name="subscriptions",
            postgresql_concurrently=True,
            if_exists=True,
        )
        op.drop_index(
            "ix_events_emitted_at",
            table_name="events",
            postgresql_concurrently=True,
            if_exists=True,
        )
        op.drop_index(
            "ux_events_idempotency_key",
            table_name="events",
            postgresql_concurrently=True,
            if_exists=True,
        )
        op.drop_index(
            "ix_events_recipient_id_cursor",
            table_name="events",
            postgresql_concurrently=True,
            if_exists=True,
        )
    op.drop_table("subscriptions")
    op.drop_table("events")
