# Multi-session attachments per agent

A single agent identity can be attached to multiple Live sessions concurrently — different labels, different cue routings, optionally one of them as the default. This is the substrate primitive that consumer-side tools (CueAPI Desktop, presence-runtime, future Monitor implementations) use to register their attached state with the server.

This page covers the data model, the four registration endpoints, the partial-unique constraints that govern label / default semantics, and the rationale for why a session-row is the right primitive.

## Why per-session rows (not per-agent JSONB)

The pre-1.0 model (`agent_shells`, deprecated and removed in v0.3.x) stored each shell as a row but framed it as a webhook delivery target. Multi-session attachments need a different shape: each row represents an attached Live session, not a delivery destination. The columns reflect this — `cue_id`, `task_name`, `label`, `is_default`, `monitor_version`, `session_token` — none of which existed on `agent_shells`.

Per-row (rather than a JSONB blob on `agents`):

- Partial unique indexes are trivial — "one default per agent" and "unique label per agent" are DB-enforced rather than app-enforced.
- Per-session updates (heartbeat, detach, default-flip) don't lock the whole agent row. High contention on the agent row is expected once consumers pull presence frequently.
- Indexable filters (`WHERE detached_at IS NULL` for "live now"; `agent_id + is_default = true` for the back-compat default-routing lookup).

## Data model

```sql
CREATE TABLE agent_live_sessions (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  agent_id        VARCHAR(20) NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  label           TEXT NOT NULL,
  cue_id          TEXT NOT NULL,
  task_name       TEXT NOT NULL,
  is_default      BOOLEAN NOT NULL DEFAULT false,
  attached_at     TIMESTAMPTZ,
  detached_at     TIMESTAMPTZ,
  last_heartbeat  TIMESTAMPTZ,
  last_claim_at   TIMESTAMPTZ,
  monitor_version TEXT,
  session_token   VARCHAR(80),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

| Column | Purpose |
|---|---|
| `id` | Internal opaque UUID. Not exposed externally — addressing is via `(agent_id, label)`. |
| `agent_id` | Foreign key to the owning agent. Type matches `agents.id` (`agt_<12 alphanum>`). |
| `label` | Per-agent identifier for this session. Required. Default convention: `"main"`. |
| `cue_id` | The cue this session claims through. Globally unique across all agents. |
| `task_name` | Canonical handler binding — must match `payload.task` verbatim on incoming fires. |
| `is_default` | Marks this session as the routing target when senders fire without specifying a label. At most one per agent. |
| `attached_at` | Server stamp at first registration. |
| `detached_at` | Soft-detach timestamp. NULL = active session. |
| `last_heartbeat` | Bumped on every heartbeat; nullable until first heartbeat lands. |
| `last_claim_at` | Bumped on every successful Live claim through this session. |
| `monitor_version` | Optional client-supplied Monitor version string. Format convention: semver-style (`v2.1.0`) for sortable string compare. Commit-SHA fallback acceptable but mixed-format comparison requires caution. |
| `session_token` | cmotigtnx attestation ULID. Crockford-base32, written by the Monitor at attach time and on Monitor restart. Used by the future `POST /v1/executions/{id}/live-claim` endpoint to cross-reference attestation tokens. |
| `created_at` | DB stamp — never updated. Audit field. |

## Constraints (partial unique indexes)

Four indexes enforce the multi-session semantics:

| Index | Columns | Predicate | Enforces |
|---|---|---|---|
| `ix_agent_live_sessions_cue_id` | `(cue_id)` | unique | cue_id is globally unique across all agents |
| `ix_agent_live_sessions_active` | `(agent_id, last_heartbeat)` | `WHERE detached_at IS NULL` | Hot-path index for the directory render |
| `ux_agent_live_sessions_one_default_per_agent` | `(agent_id)` | unique `WHERE is_default = true AND detached_at IS NULL` | At most one default-routing session per agent |
| `ux_agent_live_sessions_label_per_agent` | `(agent_id, label)` | unique `WHERE detached_at IS NULL` | Labels are unique per agent among active sessions |

The two `ux_*` partial unique indexes carry the `WHERE detached_at IS NULL` clause, which means **a session that detaches frees up its label and default slot for re-use**. This is the key correctness property for session-restart semantics — see the [re-attach pattern](#re-attaching-after-detach) below.

## Endpoints

All endpoints require authentication via `Authorization: Bearer cue_sk_...`. Cross-user agent access returns 404 (not 403) to avoid leaking existence.

### Register

```http
POST /v1/agents/{ref}/live-sessions
Content-Type: application/json

{
  "label": "main",
  "cue_id": "cue_abc123def456",
  "task_name": "owner-instance-main-live",
  "is_default": true,
  "monitor_version": "v2.1.0",
  "session_token": "01HZWC4KGE7ZYAZQX8JBQK9MPN"
}
```

Returns `201` with the registered session entry. Each session is the single-writer for its row — composite registration (one call registers all of an agent's sessions at once) is intentionally not supported. This eliminates the "which session writes the canonical full list?" concurrency question.

Errors:

| Code | Status | Cause |
|---|---|---|
| `live_session_conflict` | 409 | cue_id already in use (any agent), label already in use (this agent's active sessions), or another session of this agent is already `is_default=true` |
| `agent_not_found` | 404 | Agent doesn't exist OR is owned by another user |

### List

```http
GET /v1/agents/{ref}/live-sessions
GET /v1/agents/{ref}/live-sessions?include_detached=true
```

Returns `200` with a JSON array of session entries. Default returns only active sessions (`detached_at IS NULL`). Pass `?include_detached=true` to surface the audit trail (useful for claim-history views).

### Detach

```http
DELETE /v1/agents/{ref}/live-sessions/{label}
```

Soft-detaches the active session with this label. Sets `detached_at = now()`, sets `is_default = false`. Returns `200` with the now-detached entry; the row stays in the audit trail. Re-registering the same label is allowed afterwards (creates a fresh row).

Returns `404 live_session_not_found` if no active session has this label.

### Patch

```http
PATCH /v1/agents/{ref}/live-sessions/{label}
Content-Type: application/json

{
  "is_default": true,
  "session_token": "01HZWC9KGENEW5HJBQK9MPN77G"
}
```

Two mutable fields:

- **`is_default: true`** — atomic single-statement UPDATE flips every active session of this agent. Old default flips to `false` in the same statement as new default flips to `true`. No zero-or-two-defaults window. This is the canonical way to change the default routing target.
- **`session_token`** — rotate the attestation ULID without detaching. Use when the Monitor restarts and wants to replace the existing token.

Both can ride the same PATCH. Errors:

| Code | Status | Cause |
|---|---|---|
| `no_mutable_fields` | 400 | Empty body — at least one of `is_default=true` or `session_token` required |
| `invalid_default_flip` | 400 | `is_default: false` alone (no-op; flip another to true or DELETE this one) |
| `live_session_not_found` | 404 | No active session with this label |

`cue_id` and `task_name` are immutable post-attach. To change them, detach + re-register.

## Re-attaching after detach

The partial unique indexes use `WHERE detached_at IS NULL`, so detaching a session frees its label and default-slot. The canonical Live-attach session-restart pattern works as follows:

1. Monitor process A is running with `label="main"`, `is_default=true`, `session_token=ULID-A`.
2. Monitor A dies (terminal closes, machine sleeps, etc.). The row stays in the table; `detached_at` is still NULL — but no claims will land because nothing is listening.
3. The new Monitor process B starts. It can either:
   - Issue `DELETE /v1/agents/{ref}/live-sessions/main` to soft-detach the stale row (clean), then `POST /v1/agents/{ref}/live-sessions` with the same `label="main"`, `is_default=true`, fresh `session_token=ULID-B`. The new row inherits the routing.
   - OR, if the operator's tooling tracks `last_heartbeat` and detaches stale rows automatically, simply skip step 1 of the new Monitor's lifecycle.

Tests pin this re-attach pattern (`test_relabel_after_detach`, `test_redefault_after_detach`) — those should never regress.

## Heartbeat and live-claim attestation

Two endpoints are scoped for follow-up PRs:

- `POST /v1/agents/{ref}/live-sessions/{label}/heartbeat` — bumps `last_heartbeat` on the active row.
- `POST /v1/executions/{id}/live-claim` — Monitor-side attestation that cross-references the POSTed `(task_name, session_token)` against `agent_live_sessions` to validate that the claim came from the registered Monitor.

Both land alongside the consumer-side wire-through. See the CHANGELOG entry under `[Unreleased] > Upcoming breaking change` for the rollout sequence.

## Migration notes

The previous `agent_shells` table (introduced in pre-1.0 v0.2.x and never adopted by a production consumer) was hard-cut deprecated in migration `026_agent_live_sessions_replaces_shells.py`. No upgrade path or data migration was provided — pre-1.0 OSS explicitly allows breaking changes, and no real consumer of `agent_shells` existed. If you have downstream code targeting `/v1/agents/{ref}/shells/*`, retarget to `/v1/agents/{ref}/live-sessions/*` and update the request shape to match the schema above.

`webhook_url` and `webhook_secret` columns from the old schema are intentionally NOT preserved on the new schema. Per-session webhook fan-out delivery isn't implemented today; pre-adding columns creates implicit API surface costly to change later. If/when fan-out delivery becomes a real ask, both columns can be added back additively in a follow-up migration.

## See also

- `app/models/agent_live_session.py` — model source
- `app/routers/agent_live_sessions.py` — endpoint source
- `alembic/versions/026_agent_live_sessions_replaces_shells.py` — schema migration
- [`agents-and-metadata.md`](agents-and-metadata.md) — agent identity model
