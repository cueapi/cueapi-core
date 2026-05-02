"""Messaging primitive v1 — Message + delivery + quotas (Phase 2.11.1 of v1 spec).

Stacks on migration 020 (Identity primitive). Adds:

* ``messages`` table — ephemeral, immutable, identity-addressed
  communication unit. Spec §3 (schema) + §4 (delivery state machine)
  + §8 (idempotency).
* ``dispatch_outbox`` extension — new ``task_type`` values
  ``'deliver_message'`` / ``'retry_message'`` (mirroring existing
  ``'deliver'`` / ``'retry'`` for cue webhook delivery). Existing
  rows untouched. ``execution_id`` and ``cue_id`` become NULLABLE
  to support message-task rows (which reference a message_id in the
  payload instead of a cue execution).
* ``usage_messages_monthly`` table — per-user-per-month message
  counter, mirrors existing ``usage_monthly`` shape. Quotas separate
  from execution quotas.
* ``users.monthly_message_limit`` — per-plan quota, default 300 free.
  cueapi-core ships with a single plan; self-hosters can set their
  own per-user limits via direct DB updates or via a hosted billing
  layer if they bring one.

Backward-compat contract:

* All additions. No existing column repurposed; no data migration
  needed for existing rows in ``dispatch_outbox`` or ``users``.
* ``users.monthly_message_limit`` ships with a server-side default
  (300) and NOT NULL — backfill happens automatically via the
  default for existing users.
* Idempotency unique partial index is ``(user_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL``. The 24-hour dedup window is
  enforced at application layer + a periodic cleanup task that nulls
  out ``idempotency_key`` on rows older than 24h, freeing the key
  for reuse. PostgreSQL doesn't support ``NOW()`` in a partial-index
  predicate (IMMUTABLE constraint) so the time window can't live in
  the index itself. Documented in §8.4 of the spec.

OSS port note: the private monorepo's version of this migration
(044_messaging_messages.py) includes a ``messages.from_api_key_id``
column with an FK to ``api_keys.id`` for multi-key audit. cueapi-core
does not have multi-key scoping (no ``api_keys`` table), so that
column is omitted here. If multi-key scoping is ever ported to OSS,
a follow-up migration can ADD COLUMN messages.from_api_key_id at
that time. The messaging service layer does not use api_key_id for
any business logic.

Revision ID: 021
Revises: 020
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "021"
down_revision = "020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- 1. users.monthly_message_limit (per-plan quota, §7) -----------
    op.add_column(
        "users",
        sa.Column(
            "monthly_message_limit",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("300"),
        ),
    )

    # ---- 2. usage_messages_monthly — per-user-per-month counter --------
    # Mirrors usage_monthly shape. Dual-write to Redis + Postgres pattern
    # from existing usage_service is reused at the service layer.
    op.create_table(
        "usage_messages_monthly",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("month_start", sa.Date(), nullable=False),
        sa.Column(
            "message_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.UniqueConstraint(
            "user_id",
            "month_start",
            name="unique_user_month_messages",
        ),
    )

    # ---- 3. messages — the message primitive itself --------------------
    op.create_table(
        "messages",
        sa.Column("id", sa.String(length=20), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "from_agent_id",
            sa.String(length=20),
            sa.ForeignKey("agents.id", ondelete="SET NULL"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "to_agent_id",
            sa.String(length=20),
            sa.ForeignKey("agents.id", ondelete="SET NULL"),
            nullable=False,
            index=True,
        ),
        # Server-issued. Root messages have thread_id = self.id;
        # replies inherit the root's thread_id via reply_to lookup.
        sa.Column("thread_id", sa.String(length=20), nullable=False, index=True),
        sa.Column(
            "reply_to",
            sa.String(length=20),
            sa.ForeignKey("messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("subject", sa.String(length=255), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        # First ~200 chars of body, server-computed at create time.
        sa.Column(
            "preview",
            sa.String(length=256),
            nullable=False,
            server_default=sa.text("''"),
        ),
        sa.Column(
            "priority",
            sa.SmallInteger(),
            nullable=False,
            server_default=sa.text("3"),
        ),
        sa.Column(
            "expects_reply",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "reply_to_agent_id",
            sa.String(length=20),
            sa.ForeignKey("agents.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "delivery_state",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'queued'"),
        ),
        sa.Column(
            "message_metadata",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # Idempotency-Key header value, max 255 chars per spec §8.
        # Application enforces 24h dedup window + periodic cleanup
        # task nulls keys older than 24h to free for reuse.
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        # SHA-256 fingerprint over (to_agent_id, body, subject,
        # priority, reply_to, metadata_canonical) for body-mismatch
        # detection on Idempotency-Key reuse with different body
        # (returns 409 idempotency_key_conflict per §8.2).
        sa.Column("idempotency_fingerprint", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        # 30-day default TTL. Cleanup task transitions to 'expired'
        # once now() > expires_at.
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now() + interval '30 days'"),
        ),
        sa.CheckConstraint(
            "priority BETWEEN 1 AND 5",
            name="valid_priority",
        ),
        sa.CheckConstraint(
            "delivery_state IN ('queued','delivering','retry_ready','delivered','read','claimed','acked','expired','failed')",
            name="valid_delivery_state",
        ),
        # 32KB body cap as DB-level backstop against application-layer
        # bypass.
        sa.CheckConstraint(
            "octet_length(body) <= 32768",
            name="body_size_limit",
        ),
    )

    # Inbox-fetch query (most common path):
    #   to_agent_id + delivery_state filter + created_at DESC ordering
    op.create_index(
        "ix_messages_inbox",
        "messages",
        ["to_agent_id", "delivery_state", sa.text("created_at DESC")],
    )

    # Sent view query (sender-side):
    op.create_index(
        "ix_messages_sent",
        "messages",
        ["from_agent_id", sa.text("created_at DESC")],
    )

    # Thread reconstruction:
    op.create_index(
        "ix_messages_thread",
        "messages",
        ["thread_id", "created_at"],
    )

    # Idempotency-Key partial unique index. The 24-hour dedup window
    # can't be in the predicate (PostgreSQL IMMUTABLE constraint on
    # partial-index predicates rules out NOW()). Cleanup task nulls
    # keys older than 24h to free for reuse.
    op.execute(sa.text("""
        CREATE UNIQUE INDEX unique_user_idempotency_key
            ON messages (user_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL;
    """))

    # ---- 4. dispatch_outbox extension ----------------------------------
    # Existing ``valid_task_type`` check constraint allows only
    # ('deliver', 'retry'). Extend to include the message-task variants.
    # Also relax the NOT NULL on execution_id and cue_id for
    # message-task rows (which reference message_id in payload).
    op.alter_column("dispatch_outbox", "execution_id", nullable=True)
    op.alter_column("dispatch_outbox", "cue_id", nullable=True)
    op.drop_constraint("valid_task_type", "dispatch_outbox", type_="check")
    op.create_check_constraint(
        "valid_task_type",
        "dispatch_outbox",
        "task_type IN ('deliver', 'retry', 'deliver_message', 'retry_message')",
    )
    # Cue-task vs message-task discrimination: cue-task rows have
    # execution_id; message-task rows have message_id in payload.
    op.create_check_constraint(
        "task_payload_shape",
        "dispatch_outbox",
        """
        (task_type IN ('deliver', 'retry') AND execution_id IS NOT NULL)
        OR
        (task_type IN ('deliver_message', 'retry_message') AND payload ? 'message_id')
        """,
    )


def downgrade() -> None:
    # 4. dispatch_outbox: revert constraints + NOT NULL.
    op.drop_constraint("task_payload_shape", "dispatch_outbox", type_="check")
    op.drop_constraint("valid_task_type", "dispatch_outbox", type_="check")
    op.create_check_constraint(
        "valid_task_type",
        "dispatch_outbox",
        "task_type IN ('deliver', 'retry')",
    )
    op.alter_column("dispatch_outbox", "execution_id", nullable=False)
    op.alter_column("dispatch_outbox", "cue_id", nullable=False)

    # 3. messages
    op.execute(sa.text("DROP INDEX IF EXISTS unique_user_idempotency_key;"))
    op.drop_index("ix_messages_thread", "messages")
    op.drop_index("ix_messages_sent", "messages")
    op.drop_index("ix_messages_inbox", "messages")
    op.drop_table("messages")

    # 2. usage_messages_monthly
    op.drop_table("usage_messages_monthly")

    # 1. users.monthly_message_limit
    op.drop_column("users", "monthly_message_limit")
