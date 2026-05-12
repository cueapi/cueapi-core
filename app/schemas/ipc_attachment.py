"""Item B Phase 1 — Pydantic wire schemas for IPC attachment endpoints.

Live-delivery-v3 substrate primitive (cf. https://trydock.ai/mike/live-delivery-v3-build-hub).
Three endpoint surfaces:

* ``POST /v1/agents/<ref>/attachments`` — register an IPC attachment.
* ``DELETE /v1/agents/<ref>/attachments/<token>`` — revoke explicitly.
* ``POST /v1/agents/reconcile-attachments`` — daemon-driven boot-reconcile.

Substrate-side design owner: cueapi-primary. Joint-design lock with CMA in
the build hub; Mike Q-B ratify locked ASYNC dispatcher path 2026-05-12 ~00:38Z.

Wire shape rules baked in:

* ``ipc_session_token`` is daemon-issued ULID (26 chars typical, 32-char
  schema cap leaves room for versioned prefixes like ``v3a_<ULID>``).
  App-layer regex validates the format on POST (no DB regex CHECK per
  CueAPI convention).
* 409 ``attachment_exists`` carries ``existing_daemon_id`` +
  ``existing_last_reconciled_at`` so the daemon can distinguish
  same-daemon-prior-session (safe DELETE+re-POST) from cross-daemon
  conflict (escalate / refuse to overwrite).
* DELETE is idempotent: 204 first-time, 200 with ``{"deleted": false,
  "reason": "already_deleted"}`` on idempotent hit. Helps daemon-side
  debugging which cleanup path won the race.
* Reconcile body is a full daemon-local view; server applies atomic
  UPSERT + downgrades unmentioned-for-this-daemon rows to ``transport='poll'``
  (CMA Q-G lean — conservative; no delete on first absence). Daily
  cleanup job deletes ``transport='poll'`` rows >24h stale.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


# Application-layer ULID validator. Daemon may prefix with v<version>_
# (Crockford base32). 26-char body is the standard ULID; up to 6-char
# prefix expansion fits in VARCHAR(32).
_TOKEN_REGEX_BODY = r"[0-9A-HJKMNP-TV-Z]{26}"
_TOKEN_REGEX_PREFIX = r"(?:v[a-z0-9]+_)?"
_TOKEN_REGEX = _TOKEN_REGEX_PREFIX + _TOKEN_REGEX_BODY


def _validate_token_shape(value: str) -> str:
    """Reject malformed tokens at the wire layer (preferred to DB regex CHECK)."""
    import re

    if not isinstance(value, str) or not re.fullmatch(_TOKEN_REGEX, value):
        raise ValueError(
            "invalid ipc_session_token format — expect 26-char ULID, optional "
            "v<version>_ prefix"
        )
    return value


class AttachmentCreate(BaseModel):
    """POST /v1/agents/<ref>/attachments — daemon attaches a Live session."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Per-agent label for this attachment (`main` / `pr-watcher` / etc.).",
    )
    task_name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Local task identifier (e.g. `max-claude-code-cueapi-live`).",
    )
    ipc_session_token: str = Field(
        ...,
        min_length=26,
        max_length=32,
        description=(
            "Daemon-issued ULID identifying this attachment for fire-accept "
            "routing. App-layer validates shape; substrate stores opaque."
        ),
    )
    attached_at: Optional[datetime] = Field(
        default=None,
        description=(
            "Daemon's wall-clock at attach time (informational; server uses "
            "now() if absent)."
        ),
    )
    monitor_version: Optional[str] = Field(
        default=None,
        max_length=64,
        description="Optional capability stamp for cross-daemon-version observability.",
    )

    @field_validator("ipc_session_token")
    @classmethod
    def _check_token(cls, value: str) -> str:
        return _validate_token_shape(value)


class AttachmentResponse(BaseModel):
    """Server response on successful attach (201) — minimal echo of stored state."""

    id: str = Field(..., description="agent_live_sessions.id (UUID stringified)")
    agent_id: str
    label: str
    task_name: str
    transport: str = Field(..., description="Always `ipc` on this endpoint's 201.")
    ipc_session_token: str
    daemon_id: str
    attached_at: datetime
    last_reconciled_at: datetime
    supersedes_token: Optional[str] = Field(
        default=None,
        description=(
            "Set iff reattach displaced an existing same-(agent,label,daemon) "
            "row. Old token is invalid from this moment forward (returns 401 "
            "`token_revoked` on subsequent /outcome callbacks or DELETE)."
        ),
    )


class AttachmentExistsError(BaseModel):
    """409 ``attachment_exists`` body shape.

    Daemon distinguishes:

    * ``existing_daemon_id == my_daemon_id`` → safe DELETE+re-POST (own
      prior session forgot to clean up).
    * ``existing_daemon_id != my_daemon_id`` → escalate. User likely
      moved machines; manual confirmation needed before clobbering.

    ``existing_last_reconciled_at`` provides freshness signal — if the
    existing attachment is stale (>24h), daemon can confidently
    DELETE+re-POST without risking live-attachment overwrite.
    """

    code: str = Field(default="attachment_exists")
    existing_token: str
    existing_daemon_id: str
    existing_attached_at: datetime
    existing_last_reconciled_at: Optional[datetime] = None
    hint: str = Field(
        default=(
            "DELETE /attachments/<existing_token> first if the existing "
            "attachment is yours; escalate if existing_daemon_id != your "
            "daemon_id."
        ),
    )


class AttachmentDeleteIdempotent(BaseModel):
    """200 body for idempotent-hit DELETE.

    First-time deletes return 204 (no body); subsequent deletes return 200
    with this body so daemon-side debugging can distinguish ``I just deleted
    it`` from ``someone else already did`` (useful for tracing which cleanup
    path won the race: explicit-DELETE / reattach-supersede / heartbeat-
    stale / daemon-absence-cleanup).
    """

    deleted: bool = Field(default=False)
    reason: str = Field(default="already_deleted")


class AttachmentReconcileEntry(BaseModel):
    """One row in a daemon's reconcile batch."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(..., min_length=1, max_length=64)
    task_name: str = Field(..., min_length=1, max_length=255)
    ipc_session_token: str = Field(..., min_length=26, max_length=32)
    attached_at: datetime

    @field_validator("ipc_session_token")
    @classmethod
    def _check_token(cls, value: str) -> str:
        return _validate_token_shape(value)


class AttachmentReconcileRequest(BaseModel):
    """POST /v1/agents/reconcile-attachments — full daemon-local view.

    Daemon reports every attachment it currently holds locally; server
    applies as a single atomic UPSERT transaction:

    1. UPSERT each reported attachment as ``transport='ipc'`` with
       ``last_reconciled_at=now()``.
    2. UPDATE all rows for this daemon_id NOT in this batch →
       ``transport='poll'`` (conservative downgrade per CMA Q-G lean;
       does NOT delete on first absence).
    3. Daily cleanup job (separate) deletes ``transport='poll'`` rows
       where ``last_reconciled_at < now() - 24h``.

    No periodic server-side reconcile — server is passive; daemon drives.
    """

    model_config = ConfigDict(extra="forbid")

    daemon_id: UUID = Field(
        ...,
        description=(
            "Stable per-install daemon identity. Must match the "
            "X-CueAPI-Daemon-Id header (validated server-side)."
        ),
    )
    reconciled_at: datetime = Field(
        ...,
        description=(
            "Daemon's wall-clock at reconcile time (informational; server "
            "uses now() for last_reconciled_at column values)."
        ),
    )
    attachments: List[AttachmentReconcileEntry] = Field(
        default_factory=list,
        description=(
            "Full daemon-local attachment list. Empty list means daemon "
            "reports zero attachments → all daemon's rows downgrade to "
            "transport='poll'."
        ),
    )


class AttachmentReconcileResponse(BaseModel):
    """200 body for /reconcile-attachments — daemon sees what server did."""

    daemon_id: str
    reconciled_at: datetime
    upserted_count: int = Field(
        ...,
        description="Rows inserted-or-updated to transport='ipc'.",
    )
    downgraded_count: int = Field(
        ...,
        description=(
            "Rows for this daemon NOT in the batch — downgraded to "
            "transport='poll' (still queryable for poll-based delivery; "
            "cleaned up after 24h stale by the daily job)."
        ),
    )
