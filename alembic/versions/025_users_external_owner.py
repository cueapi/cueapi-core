"""Add ``external_owner`` column to ``users`` for consumer-attribution audit.

Records which integrator minted the User row when the row was created
via ``PUT /v1/internal/users/{id}`` (Path 2 / internal-token auth flow,
see ``docs/internal-token-auth.md``). NULL for self-mint via
``/v1/auth/register`` and for User rows created before this migration.

Per CWS-2026-05-08 Tier 2 lock (refined):
* OSS (this codebase) — single-key shape; attribution at User level.
* Private (cueapi-hosted) — multi-key shape; attribution at api_key
  level. Symmetric concept, different schema shape because OSS lacks
  the api_keys table (multi-key scoping is hosted-only per
  HOSTED_ONLY.md).

Field semantics:
* Type: ``VARCHAR(64)``, NULL allowed
* Set: integrator stamps on first ``PUT /v1/internal/users/{id}`` for
  a User they're minting. Convention: short lowercase prefix matching
  the consumer's tag (``"dock"``, ``"obs"``, ``"cd"``, etc.). Substrate
  treats as opaque.
* Read: not exposed in user-facing API surfaces; audit-only. Read by
  operators via direct DB query or the (forthcoming) operator
  observability surface.
* Mutability: integrators MAY update via subsequent PUT calls (idempotent
  upsert). Substrate doesn't enforce immutability.

Why VARCHAR(64) and not larger:
* Convention is short prefix (~3-12 chars). 64 chars is generous headroom
  + matches the size of other `_owner`-style audit fields in private
  cueapi for ergonomic parity.

No index. Audit field; not used as a query predicate at request time.
If operators want to query "all users minted by Dock," a sequential scan
is fine for the cardinality (admin-tool tier, not hot path).

Revision ID: 025
Revises: 024
"""
from alembic import op
import sqlalchemy as sa


revision = "025"
down_revision = "024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("external_owner", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "external_owner")
