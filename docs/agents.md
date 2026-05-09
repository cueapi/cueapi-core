# Agents

Agents are the addressable identities in CueAPI's messaging primitive. They are first-class records: every message has a `from_agent_id` and a `to_agent_id`, every webhook delivery is keyed by an agent, and every cross-product address (`scout@govind`) resolves to an agent.

This page covers the agent lifecycle (create / read / update / soft-delete), the address-resolution surface, and webhook secret management. For the metadata convention (well-known keys + consumer-namespacing), see [`agents-and-metadata.md`](agents-and-metadata.md). For inbox / sent operations on a particular agent, see the messaging spec.

## Why agents are first-class

Cues are scheduled work. Messages are direct comms. Both need a recipient. CueAPI Core models that recipient as an agent — a stable, addressable identity that:

- Owns a webhook URL (or stays poll-only via `null`).
- Carries opaque metadata for consumer-side categorization.
- Can be addressed by both opaque ID (`agt_abc123`) and human-readable slug-form (`scout@govind`).
- Survives the deletion of the User who owns it for 30 days (soft-delete tombstone), preserving message history during the wind-down window.

A User can own many agents. Common patterns: one agent per process / runtime / workspace / consumer-product surface. There is no upper bound enforced by the substrate; consumers cap at the SaaS layer if they want.

## Identity model

Three identifiers, three address shapes:

| Identifier | Format | Stability | Use |
|---|---|---|---|
| `id` | `agt_<12 alphanumeric>` | Permanent. Never reused. | Canonical cross-product anchor; primary key on the `agents` table; FK in `messages.from_agent_id` and `messages.to_agent_id`. |
| `slug` | 1-64 chars, lowercase + digits + hyphens; matches `^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$` | Updatable via `PATCH` only on user_id slug uniqueness; cross-user collisions impossible by `UNIQUE(user_id, slug)`. | Human-readable handle; appears in `slug-form` addresses. |
| `display_name` | 1-255 chars; arbitrary text | Mutable. | Rendering surface (UI cards, chat bubbles, etc.). Substrate doesn't normalize. |

### Address shapes

The substrate accepts three address forms wherever an agent is referenced (`{ref}` path parameters, `to` field in `POST /v1/messages`, etc.):

| Shape | Example | Resolution |
|---|---|---|
| Opaque ID | `agt_abc123def456` | Direct lookup by primary key. Canonical for substrate operations. |
| Slug-form (with user) | `scout@govind` | Resolves via `User.slug == "govind"` AND `Agent.slug == "scout"` AND `User.user_id == Agent.user_id`. |
| Bare slug (current user) | `scout` | Resolves the slug under the authenticated User's namespace. Ambiguous outside an authenticated context — only valid where the substrate already knows which User to scope by (e.g. `GET /v1/agents/scout/inbox` resolves under the auth principal's user_id). |

The opaque ID is canonical. Slug-form addresses are translated to opaque IDs before any substrate-level operation. Cross-system routing, audit logs, and persisted references should always use the opaque form; slug-form is for human readability.

## Lifecycle

Agents are created, read, soft-deleted, and (after 30 days) hard-deleted. Update is supported via `PATCH` for `display_name`, `webhook_url`, `status`, and `metadata`; `slug` is set-once-then-locked.

```
POST /v1/agents                 → create (returns webhook_secret if webhook_url given)
GET  /v1/agents                 → list (paginated, filterable by status + include_deleted)
GET  /v1/agents/{ref}           → read one
PATCH /v1/agents/{ref}          → update display_name / webhook_url / status / metadata
DELETE /v1/agents/{ref}         → soft-delete (sets deleted_at; row preserved 30 days)
```

### Soft-delete semantic

`DELETE /v1/agents/{ref}` returns `204 No Content` and sets `deleted_at` to the current timestamp. The row is preserved for 30 days so:

1. Messages whose `from_agent_id` or `to_agent_id` reference this agent stay readable.
2. The address (`scout@govind`) stops resolving — subsequent sends to that address return `404 agent_not_found`.
3. Cross-product references that hold the opaque `agt_xxx` ID can still resolve via `GET /v1/agents/{ref}?include_deleted=true` for audit purposes.

After 30 days, a background cleanup job hard-deletes the row. `ON DELETE SET NULL` on the message FKs preserves message history with `from_agent_id` / `to_agent_id` set to `null`.

To bring back a soft-deleted agent, the substrate doesn't currently support undelete. The integrator's pattern is: soft-delete the old, create a new agent with the same slug (allowed once `deleted_at` is set; the unique constraint `UNIQUE(user_id, slug, deleted_at IS NULL)` is partial). Cross-product references won't carry over; the new agent gets a fresh `agt_xxx`.

### Status field

`status` is `online` / `offline` / `away`. Substrate-managed for push-delivery agents (set automatically based on webhook health) but consumer-settable via `PATCH /v1/agents/{ref}` for poll-only agents. Three values, no extension; if you need richer presence, encode it in `metadata` per the [agents-and-metadata convention](agents-and-metadata.md).

## CRUD endpoint reference

### `POST /v1/agents` — Create

```http
POST /v1/agents HTTP/1.1
Authorization: Bearer cue_sk_...
Content-Type: application/json

{
  "slug": "scout",
  "display_name": "Scout",
  "webhook_url": "https://my.example.com/cueapi/webhook",
  "metadata": {"kind": "agent", "myproduct.team": "engineering"}
}
```

Required:

- `display_name` — 1-255 chars.

Optional:

- `slug` — 1-64 chars, regex `^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$`. If omitted, the server derives from `display_name` (lowercase + hyphenate + truncate). If the derived slug collides with an existing one under your User, the server appends a numeric suffix.
- `webhook_url` — push-delivery target. SSRF-validated (substrate rejects internal IPs, private ranges, `localhost`, etc.). `null` or omit = poll-only agent.
- `metadata` — opaque JSONB. See [`agents-and-metadata.md`](agents-and-metadata.md) for the convention.

Response (`201 Created`):

```json
{
  "id": "agt_abc123def456",
  "user_id": "5b2e7c3a-9d11-4a8f-b6e2-9c4d3f1a8b7c",
  "slug": "scout",
  "display_name": "Scout",
  "webhook_url": "https://my.example.com/cueapi/webhook",
  "webhook_secret": "whsec_xyz789...",
  "metadata": {"kind": "agent", "myproduct.team": "engineering"},
  "status": "online",
  "deleted_at": null,
  "created_at": "2026-05-09T20:00:00Z",
  "updated_at": "2026-05-09T20:00:00Z"
}
```

The `webhook_secret` field is **populated only on this `POST` response** (and on the rotation endpoint below) — subsequent reads omit it. Persist it immediately; the substrate doesn't return it again unless you regenerate.

If `webhook_url` was null/omitted, `webhook_secret` is `null` in the response (no push-delivery target = no secret to issue).

### `GET /v1/agents` — List

```http
GET /v1/agents?status=online&include_deleted=false&limit=50&offset=0 HTTP/1.1
Authorization: Bearer cue_sk_...
```

Query parameters:

| Param | Default | Description |
|---|---|---|
| `status` | _(no filter)_ | One of `online`, `offline`, `away`. Anything else returns `400 invalid_status`. |
| `include_deleted` | `false` | Set `true` to include soft-deleted rows in the result (within the 30-day tombstone window). |
| `limit` | 50 | 1-100. |
| `offset` | 0 | Pagination cursor. |

Response (`200 OK`):

```json
{
  "agents": [
    {"id": "agt_...", "slug": "scout", "...": "..."},
    {"id": "agt_...", "slug": "self", "...": "..."}
  ],
  "total": 2,
  "limit": 50,
  "offset": 0
}
```

`webhook_secret` is `null` on list responses for all agents.

### `GET /v1/agents/{ref}` — Read one

`{ref}` accepts opaque ID (`agt_xxx`), slug-form (`slug@user`), or bare slug under the authenticated User.

```http
GET /v1/agents/agt_abc123def456 HTTP/1.1
Authorization: Bearer cue_sk_...
```

Response (`200 OK`): full `AgentResponse` with `webhook_secret` set to `null`.

`404 agent_not_found` if the slug doesn't resolve, or the agent is soft-deleted and `?include_deleted=true` wasn't set.

### `PATCH /v1/agents/{ref}` — Update

```http
PATCH /v1/agents/agt_abc123def456 HTTP/1.1
Authorization: Bearer cue_sk_...
Content-Type: application/json

{
  "display_name": "Scout (renamed)",
  "metadata": {"kind": "agent", "myproduct.team": "growth"}
}
```

Updatable fields:

| Field | Semantic |
|---|---|
| `display_name` | Replace. Required to be non-empty if provided. |
| `webhook_url` | Set or clear. Pass `null` explicitly to clear (which switches the agent to poll-only). Pass a value to set. Omit to leave unchanged. |
| `status` | Replace. One of `online` / `offline` / `away`. |
| `metadata` | **Whole-replace, not deep-merge.** See [`agents-and-metadata.md`](agents-and-metadata.md#update-semantics) for the read-merge-PATCH pattern. |

Not updatable: `slug` (set-once-then-locked), `id`, `user_id`, `webhook_secret`, `deleted_at`, `created_at`. Attempting to include any of these returns `422 validation_error` (the request schema sets `extra="forbid"`).

Response: `200 OK` with the updated full `AgentResponse` (webhook_secret null).

### `DELETE /v1/agents/{ref}` — Soft-delete

```http
DELETE /v1/agents/agt_abc123def456 HTTP/1.1
Authorization: Bearer cue_sk_...
```

Response: `204 No Content`. Sets `deleted_at = now()`. The row is preserved for 30 days.

## Webhook secret management

When an agent has a `webhook_url`, the substrate signs every push delivery with HMAC-SHA256 keyed on the agent's `webhook_secret` (and includes `X-CueAPI-Signature` + `X-CueAPI-Timestamp` headers). See [`webhook-verification.md`](webhook-verification.md) for the verification recipe.

Three flows touch the secret:

1. **Mint** — set automatically when an agent is created with a non-null `webhook_url`. Returned exactly once on the `POST /v1/agents` response.
2. **Retrieve** — `GET /v1/agents/{ref}/webhook-secret`. Returns the current secret. Use this if you lost the original mint response.
3. **Rotate** — `POST /v1/agents/{ref}/webhook-secret/regenerate`. Mints a fresh secret + drops the old one immediately. Requires the `X-Confirm-Destructive: true` header.

### `GET /v1/agents/{ref}/webhook-secret` — Retrieve

```http
GET /v1/agents/agt_abc123def456/webhook-secret HTTP/1.1
Authorization: Bearer cue_sk_...
```

Response (`200 OK`):

```json
{"webhook_secret": "whsec_xyz789..."}
```

`404 agent_not_found` if the agent doesn't resolve. `404 webhook_not_configured` if the agent is poll-only (no `webhook_url` set).

### `POST /v1/agents/{ref}/webhook-secret/regenerate` — Rotate

```http
POST /v1/agents/agt_abc123def456/webhook-secret/regenerate HTTP/1.1
Authorization: Bearer cue_sk_...
X-Confirm-Destructive: true
```

The `X-Confirm-Destructive: true` header is required. Without it, the substrate returns `400 confirmation_required` and the rotation does not happen. Same pattern as `POST /v1/auth/key/regenerate`.

Response (`200 OK`):

```json
{"webhook_secret": "whsec_new_value..."}
```

The old secret stops verifying immediately. Any in-flight webhook deliveries already signed with the old secret will fail signature verification on the consumer side. Coordinate rotation with consumer-side updates if you need zero-downtime rotation; substrate doesn't support overlap windows.

## Address-resolution behavior

`{ref}` path parameters resolve via the substrate's address resolver:

1. If `{ref}` matches `agt_<12 alphanumeric>`, look up by `id`.
2. If `{ref}` contains `@`, split on `@` into (`agent_slug`, `user_slug`); look up the user by slug, then the agent by `(user_id, slug)`.
3. Otherwise, treat `{ref}` as a bare slug under the authenticated User; look up by `(user_id, slug)`.

Failures:

- Malformed slug (regex doesn't match): `400 invalid_slug` or `404 agent_not_found` depending on the path.
- Slug not found under the resolved User: `404 agent_not_found`.
- User not found (slug-form with unknown user_slug): `404 agent_not_found` (the substrate doesn't distinguish "no such user" from "no such agent under that user" to avoid enumeration leaks).

## Error responses

| Code | Status | Cause |
|---|---|---|
| `invalid_slug` | 400 | Slug doesn't match regex `^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$`. |
| `invalid_status` | 400 | `?status=` query param isn't one of `online` / `offline` / `away`. |
| `confirmation_required` | 400 | Destructive action (webhook-secret rotate) without `X-Confirm-Destructive: true`. |
| `validation_error` | 422 | Request body validation failed. Includes field-level details. |
| `agent_not_found` | 404 | `{ref}` doesn't resolve, OR the agent is soft-deleted and `?include_deleted=true` wasn't set. |
| `webhook_not_configured` | 404 | `webhook-secret` retrieval/rotate on a poll-only agent. |
| `agent_slug_in_use` | 409 | `POST /v1/agents` with a slug already taken under your User. |

## What metadata is for (cross-link)

`metadata` is opaque JSONB the substrate writes through but never interprets. Use it for consumer-side categorization (`metadata.kind = "agent"`), product-namespaced fields (`metadata.myproduct.workspace_id`), and anything else specific to your product surface.

The full convention — well-known keys, consumer-namespacing, the `agent_metadata` DB-column workaround, the whole-replace PATCH semantic — lives in [`agents-and-metadata.md`](agents-and-metadata.md).

## See also

- [`agents-and-metadata.md`](agents-and-metadata.md) — metadata convention (well-known keys + namespacing)
- [`webhook-verification.md`](webhook-verification.md) — webhook signature verification recipe (uses the agent's `webhook_secret`)
- [`internal-token-auth.md`](internal-token-auth.md) — Path 2 auth (integrators creating agents on behalf of users)
- [`authorization-backends.md`](authorization-backends.md) — substrate-side authorization hook (orthogonal to agent identity)
- [`quickstart.md`](quickstart.md) — first agent + first message walkthrough
