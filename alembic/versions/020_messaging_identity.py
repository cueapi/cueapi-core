"""Messaging primitive v1 — Identity (Phase 2.11.1 of the v1 spec).

Adds the ``agents`` table (per-user addressable identities) and the
``users.slug`` column (per-tenant slug used in the ``agent@user``
slug-form addressing convention from §6 of the spec).

Schema overview:

* ``agents`` — one row per addressable Identity. Format ``agt_<12 alphanum>``
  primary key (mirrors the existing ``cue_<12>`` convention from
  ``app/utils/ids.py``). Per-user scoping via ``user_id`` FK.
* ``users.slug`` — VARCHAR(64) per-tenant unique slug. Auto-derived
  from ``email.split('@')[0]`` lower-cased + non-alphanumerics replaced
  with hyphens + collision-suffixed. Patchable via
  ``PATCH /v1/auth/me`` subject to lock-after-set semantics.

Backfill: deterministic per ROW_NUMBER() over the derived base slug
to guarantee uniqueness even when multiple existing users have the
same email-local-part. First user (by created_at, id) keeps the bare
slug; subsequent users get an id-prefix suffix.

Backward-compat contract:

* Both new tables/columns are additive. No existing column or row
  changed in a load-bearing way.
* ``users.slug`` ships nullable in the ALTER, gets backfilled in the
  same migration, then immediately ``SET NOT NULL`` and ``UNIQUE``
  applied — at the end of the upgrade no row has a NULL slug.
* Downgrade is full reversal: drops ``agents``, drops the unique
  constraint and NOT NULL on ``users.slug``, then drops the column.

OSS port note: the private monorepo's version of this migration
(043_messaging_identity.py) includes an ``agents.api_key_id`` column
with an FK to ``api_keys.id`` for multi-key scoping. cueapi-core does
not have multi-key scoping (no ``api_keys`` table), so that column is
omitted here. If multi-key scoping is ever ported to OSS, a follow-up
migration can ``ADD COLUMN agents.api_key_id`` with the FK at that
time. The messaging service layer does not use ``api_key_id`` for any
business logic — it was an audit-only field in the private version.

Revision ID: 020
Revises: 019
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "020"
down_revision = "019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- 1. users.slug column -----------------------------------------
    # Add as NULLABLE first so the table-rewrite is fast and we have a
    # single transaction to backfill in.
    op.add_column(
        "users",
        sa.Column("slug", sa.String(length=64), nullable=True),
    )

    # Backfill: deterministic, collision-safe. ROW_NUMBER over the
    # derived base slug (lower-cased email-local-part with non-alphanum
    # → hyphens, leading/trailing hyphens trimmed, empty fallback to
    # 'user') ensures the first user (by created_at, id) keeps the
    # bare slug and subsequent users with the same base get a 4-char
    # id-prefix suffix.
    op.execute(sa.text("""
        WITH derived AS (
            SELECT
                id,
                created_at,
                COALESCE(
                    NULLIF(
                        TRIM(BOTH '-' FROM
                            regexp_replace(
                                lower(split_part(email, '@', 1)),
                                '[^a-z0-9]+',
                                '-',
                                'g'
                            )
                        ),
                        ''
                    ),
                    'user'
                ) AS base
            FROM users
        ),
        numbered AS (
            SELECT
                id,
                base,
                ROW_NUMBER() OVER (PARTITION BY base ORDER BY created_at, id) AS rn
            FROM derived
        )
        UPDATE users
        SET slug = CASE
            WHEN numbered.rn = 1 THEN numbered.base
            ELSE numbered.base || '-' || substr(numbered.id::text, 1, 4)
        END
        FROM numbered
        WHERE users.id = numbered.id;
    """))

    # Now SET NOT NULL + add UNIQUE constraint. Backfill above
    # guarantees no NULL rows and no duplicates.
    op.alter_column("users", "slug", nullable=False)
    op.create_unique_constraint("unique_user_slug", "users", ["slug"])

    # ---- 2. agents table ----------------------------------------------
    op.create_table(
        "agents",
        sa.Column("id", sa.String(length=20), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        # Push-delivery target. Validated for SSRF on set/update via
        # app.utils.url_validation.validate_callback_url. NULL = poll-only.
        sa.Column("webhook_url", sa.Text(), nullable=True),
        # HMAC-SHA256 signing secret for push deliveries. Generated on
        # first webhook_url set (matches existing User.webhook_secret
        # ``whsec_<64 hex>`` shape). NULL when webhook_url is NULL.
        sa.Column("webhook_secret", sa.String(length=80), nullable=True),
        sa.Column(
            "agent_metadata",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'online'"),
        ),
        # Soft-delete tombstone. Hard-delete runs 30 days later via the
        # cleanup task.
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("user_id", "slug", name="unique_user_agent_slug"),
        sa.CheckConstraint(
            "status IN ('online', 'offline', 'away')",
            name="valid_agent_status",
        ),
        # webhook_url and webhook_secret are set/unset together —
        # neither both null nor both set is invalid; only the
        # mismatched-pair states are rejected.
        sa.CheckConstraint(
            "(webhook_url IS NULL) = (webhook_secret IS NULL)",
            name="agent_webhook_url_secret_paired",
        ),
    )

    # Most-common inbox-resolution query: live agents by user + status.
    op.execute(sa.text("""
        CREATE INDEX ix_agents_user_status_active
            ON agents (user_id, status)
            WHERE deleted_at IS NULL;
    """))

    # Cross-user slug lookup for slug-form addressing (`agent@user`).
    # Per-user uniqueness is enforced by the composite UNIQUE above;
    # this index speeds the addressing-resolver join.
    op.create_index("ix_agents_slug", "agents", ["slug"])


def downgrade() -> None:
    op.drop_index("ix_agents_slug", "agents")
    op.execute(sa.text("DROP INDEX IF EXISTS ix_agents_user_status_active;"))
    op.drop_table("agents")

    op.drop_constraint("unique_user_slug", "users", type_="unique")
    op.alter_column("users", "slug", nullable=True)
    op.drop_column("users", "slug")
