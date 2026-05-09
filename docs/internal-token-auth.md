# Internal-token auth & agent attribution

cueapi-core ships **two authentication paths** for incoming HTTP requests. Most operators run only the first; integrators that proxy on behalf of multiple users (chat products, workspace tools, multi-tenant deployments) opt into the second.

This page covers both paths, when to use which, the wire format, the security model, and integration patterns.

## The two paths

### Path 1 — per-User API key (default)

The default path. Each User row carries a hashed API key. Clients send `Authorization: Bearer cue_sk_...`; the substrate looks up the key hash, returns the matching User. One key per User.

```
Authorization: Bearer cue_sk_abcd1234efgh5678
```

This is the path that:

- Self-hosters use for direct CLI / SDK / curl calls.
- Single-tenant deployments use for everything.
- Daemon-style consumers (e.g. a per-agent poller running on a user's machine) use to call the substrate directly.

No configuration required — Path 1 is always enabled.

### Path 2 — internal token + `X-On-Behalf-Of` (for integrators)

When the operator sets `EXTERNAL_AUTH_BACKEND=true` and provides an `INTERNAL_AUTH_TOKEN`, a *second* authentication path becomes available. Clients send the shared internal token in the standard `Authorization` header **plus** an `X-On-Behalf-Of` header naming which User the request acts as.

```
Authorization: Bearer <INTERNAL_AUTH_TOKEN>
X-On-Behalf-Of: <user-uuid>
```

This is the path that:

- Server-side integrator backends use when proxying multiple end-users to the substrate (Dock's full integration — `mirrorAgentToCue` and every dock-live message hop — runs through Path 2; an enterprise app server forwarding employee actions; etc.).
- Multi-tenant deployments use when the integrator has its own user database and wants to map external users to cueapi User rows on demand.

Path 2 is **opt-in**. If `EXTERNAL_AUTH_BACKEND` is unset or `INTERNAL_AUTH_TOKEN` is empty, Path 2 is unreachable — every request falls through to Path 1.

## When to use which

| Use case | Path |
|---|---|
| Solo developer running cueapi-core locally | Path 1 |
| One product, one user-database matching cueapi's | Path 1 |
| Daemon polling on behalf of a single User | Path 1 |
| Multi-user product proxying messages to the substrate | Path 2 |
| Integrator with its own user-store + permission model | Path 2 |
| Per-request "who is this for" attribution needed | Path 2 |
| Mixed: backend uses Path 2 directly, separate per-User daemons use Path 1 | Both |

A single deployment can use **both paths simultaneously**. Whether to mix depends on whether per-User daemons exist and authenticate to cueapi-core directly with their own `cue_sk_...` keys, or whether all calls (including those originating from per-User daemons) flow through the integrator backend. The substrate accepts either; the choice is per-request.

> **Path 2 only, end-to-end (Dock's setup).** Dock is a Path 2 consumer at every hop: end-user, dock-live daemon, and dock-app-server all reach cueapi-core through Dock's app server with `INTERNAL_AUTH_TOKEN` + `X-On-Behalf-Of`. The dock-live daemon polls **Dock's** `/api/dock-connect/inbox` with a Dock-issued `dk_*` Bearer token; that token never reaches cueapi-core. Dock's app server then proxies to cueapi-core via Path 2. See [Pattern A](#pattern-a--server-side-proxy-dock-shape) for the canonical reference.

## Configuration

Two environment variables enable Path 2:

```sh
EXTERNAL_AUTH_BACKEND=true
INTERNAL_AUTH_TOKEN=<long-random-secret>
```

Generate the token with strong entropy. Recommended:

```sh
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Both must be set. With either unset, Path 2 is disabled.

The token is compared with `hmac.compare_digest` to defend against timing-based extraction attempts.

## User-row management for Path 2

Path 2 callers must reference a User UUID via `X-On-Behalf-Of`. The substrate verifies that User row exists. If it doesn't, the request fails 404 — so the integrator must **upsert** the User row first.

The canonical endpoint for that upsert is `PUT /v1/internal/users/{user-uuid}`. This is the only sanctioned way for an integrator to mint or update User rows in cueapi-core; the standard `POST /v1/auth/register` flow (Path 1) is independent and produces self-mint Users that aren't backed by an external identity system.

### Endpoint reference

```http
PUT /v1/internal/users/{user-uuid} HTTP/1.1
Authorization: Bearer <INTERNAL_AUTH_TOKEN>
Content-Type: application/json
```

| Field | Type | Required | Default (on insert) | Notes |
|---|---|---|---|---|
| `email` | string | yes | — | Validated as RFC-5322 email. Updatable on subsequent calls. |
| `slug` | string | yes | — | Regex: `^[a-z0-9][a-z0-9-]*[a-z0-9]$` (or single character). 1–64 chars. Used in slug-form addressing (`agent@<slug>`). Updatable on subsequent calls. |
| `plan` | string | no | `"free"` | Free-form plan label (consumer-defined; substrate doesn't validate values). |
| `active_cue_limit` | int ≥ 0 | no | 10 | |
| `monthly_execution_limit` | int ≥ 0 | no | 300 | |
| `monthly_message_limit` | int ≥ 0 | no | 300 | |
| `rate_limit_per_minute` | int ≥ 0 | no | 60 | Per-User rate limit applied at the request middleware. |
| `external_owner` | string ≤ 64 chars | no | `null` | Audit-only attribution tag (e.g. `"dock"`, `"obs"`, `"cd"`). Records which integrator minted the User row. Substrate treats as opaque. See [`agents-and-metadata.md`](agents-and-metadata.md) for the broader namespacing convention. |

### Response

```json
{
  "id": "5b2e7c3a-9d11-4a8f-b6e2-9c4d3f1a8b7c",
  "email": "user@integrator.example",
  "slug": "integrator-user-slug",
  "plan": "free",
  "active_cue_limit": 10,
  "monthly_execution_limit": 300,
  "monthly_message_limit": 300,
  "rate_limit_per_minute": 60,
  "created": true
}
```

The `created` flag is the only field that varies based on the operation:

- `created: true` — a new row was inserted on this call.
- `created: false` — an existing row was updated (or matched the request exactly — no-op).

Integrators that need to detect "first-time User mint" (e.g. for welcome-email triggers) can branch on this field. Otherwise it can be ignored.

### Insert vs update semantics

The endpoint is idempotent. Calling it repeatedly is safe.

**On insert** (no row exists for the UUID): all required fields are written; optional fields not in the request body fall back to defaults from the table above.

**On update** (row exists): each field is omit-preserves-prior. Including a field with a value updates that field; omitting the field leaves the existing value untouched. There is no way to "clear" a field back to its default via this endpoint — issue a follow-up call with the explicit value if needed. (`null` in the body is interpreted as "no override," not "clear to null," for the optional fields.)

`email` and `slug` are required on every call. To "rename" a User's slug or change their email, simply issue a PUT with the new values; the existing row is updated in place.

### Generated stub API key

Newly-minted User rows include a stub `api_key_hash` (the column is `NOT NULL` for Path 1 compatibility), but the plaintext key is **never returned** via this endpoint. Path 2 callers don't need it.

If the integrator later wants this User to have a usable Path 1 API key (e.g. to give them a CLI handoff), they can:

1. Issue `POST /v1/auth/key/regenerate` to mint a fresh key + retire the stub. The new plaintext is returned once.
2. Hand the plaintext key to the end-user out-of-band.

This is a one-way transition: the User row is now usable on both paths until the next regenerate.

### Idempotency

Idempotent — calling with the same UUID + body is a no-op (returns the existing row, `created: false`). Required exactly once per User before issuing Path 2 requests against that UUID; subsequent message/agent operations don't re-call.

For consumers building automation (e.g. nightly user-sync from an external IDP), it is safe to bulk-PUT all known Users on every sync — only changed rows produce visible state changes.

### Error responses (endpoint-specific)

| Code | Status | Cause |
|---|---|---|
| `invalid_internal_token` | 401 | Missing or invalid `Authorization: Bearer ...` header — token didn't match `INTERNAL_AUTH_TOKEN`. |
| `invalid_user_id` | 400 | Path parameter `{user-uuid}` isn't parseable as a UUID. |
| `validation_error` | 422 | Request body validation failed (e.g. invalid email format, slug doesn't match regex, integer field below 0, `external_owner` over 64 chars). |

For the broader Path 2 error surface (header validation, missing User refs, etc.), see [Path 2 error responses](#path-2-error-responses) below.

### Cross-product attribution example

A multi-product integrator can use `external_owner` to distinguish which product minted each User:

```http
PUT /v1/internal/users/abcd1234-... HTTP/1.1
Authorization: Bearer <INTERNAL_AUTH_TOKEN>
Content-Type: application/json

{
  "email": "alice@dock.example",
  "slug": "alice",
  "external_owner": "dock"
}
```

Operators can then audit who minted what via direct DB query (`SELECT id, email, slug, external_owner FROM users WHERE external_owner = 'dock'`). The field is not currently exposed in user-facing API responses; surface for operator-tooling only.

## Wire format reference

### Path 1 request

```http
GET /v1/agents/me/inbox HTTP/1.1
Host: cue.example.com
Authorization: Bearer cue_sk_abcd1234efgh5678
```

### Path 2 request

```http
POST /v1/messages HTTP/1.1
Host: cue.example.com
Authorization: Bearer 1f9e8d7c6b5a4938... (INTERNAL_AUTH_TOKEN)
X-On-Behalf-Of: 5b2e7c3a-9d11-4a8f-b6e2-9c4d3f1a8b7c
Content-Type: application/json

{"to": "agent@user", "body": "..."}
```

### Path 2 error responses

| Code | Status | Cause |
|---|---|---|
| `internal_token_requires_on_behalf_of` | 400 | Internal token matched but `X-On-Behalf-Of` header was missing |
| `invalid_on_behalf_of` | 400 | `X-On-Behalf-Of` value isn't a valid UUID |
| `user_not_found` | 404 | UUID in `X-On-Behalf-Of` doesn't match an upserted User row |
| `invalid_api_key` | 401 | Token didn't match `INTERNAL_AUTH_TOKEN` (and isn't a valid Path 1 API key either) |

## Security model

Path 2 trades request-level authentication for service-level trust. The integrator's service is implicitly authorized to act as **any** User it asserts via `X-On-Behalf-Of` — there's no per-request user secret. Implications:

- **Treat `INTERNAL_AUTH_TOKEN` as a shared service secret.** Rotation cadence should match other infrastructure secrets (e.g. quarterly, plus immediately on suspected compromise). Do NOT commit it to repositories or log it; do NOT pass it through frontend code; do NOT distribute it to end-users.
- **Authorization happens at the integrator's boundary.** The substrate trusts the integrator's claim that the asserted User is the right one. The integrator is responsible for verifying the actual end-user (via OAuth, session token, etc.) BEFORE forwarding the request with `X-On-Behalf-Of`. Any failure of integrator-side authz becomes an authentication bypass on the substrate.
- **Audit the integrator service surface.** Every Path-2-enabled deployment should have integrator-side logging: every `X-On-Behalf-Of` value sent, the source request that produced it, the authenticated end-user. Without this, after-the-fact accountability is impossible.
- **Path 2 doesn't bypass authorization checks.** The `AuthorizationBackend` hook (see `docs/authorization-backends.md`) still runs on every message send. Path 2 is auth, not authz.

## Integration patterns

### Pattern A — server-side proxy (Dock-shape)

The integrator runs a backend service. End-users authenticate to the backend (OAuth / session tokens / whatever). The backend proxies all cueapi operations to cueapi-core using Path 2.

```
end-user → integrator backend → cueapi-core
            (OAuth)             (INTERNAL_AUTH_TOKEN
                                 + X-On-Behalf-Of: <user-uuid>)
```

Best for: chat products, workspace tools, multi-tenant SaaS where the integrator owns the user-database and identity model.

This is the **complete shape** for Dock — end-to-end Path 2, including their per-User daemon (`dock-live`). The daemon polls Dock's own `/api/dock-connect/*` endpoints (not cueapi-core directly) with a Dock-issued `dk_*` Bearer token. Dock's app server then proxies to cueapi-core with `INTERNAL_AUTH_TOKEN` + `X-On-Behalf-Of`. The `dk_*` token never reaches cueapi-core. Reference implementation in `dock-app/src/app/api/dock-connect/`:

- `inbox/route.ts:209` — inbox polling proxy
- `shells/route.ts:115` — agent shell registration proxy
- `shells/[shellId]/heartbeat/route.ts:93` — heartbeat proxy
- `ack/route.ts:83` — message acknowledgment proxy

Each authenticates the caller via Dock's session/key, looks up Dock agent → mirrored cueapi identity, then proxies to cueapi-core via Path 2.

### Pattern B — daemon-style poller (Path 1 direct)

A long-running daemon (Go binary, Python script, etc.) polls cueapi-core directly using a per-User Path 1 key. The daemon runs on the User's own machine; the API key was issued to that User and is treated as a machine credential.

```
daemon-on-user-machine → cueapi-core
   (cue_sk_... per User)
```

Best for: per-machine push delivery, presence tracking, single-User scope. Avoids any server-side proxy hop. The CueAPI Desktop bundled-app daemon and the open-source `cueapi-worker` package are reference consumers of this pattern. (Dock's `dock-live` daemon uses a similar shape *to Dock's own server*, but it is NOT a Path 1 consumer of cueapi-core — see Pattern A.)

### Pattern C — hybrid (mixed Path 1 + Path 2)

A deployment can mix paths if it has both a backend integrator surface AND per-User daemons that authenticate to cueapi-core directly with their own keys. Server-side actions (creating agents, sending on behalf of users from a backend) flow through Path 2; per-machine daemons that own a `cue_sk_...` key use Path 1.

```
end-user → integrator backend → cueapi-core   (Path 2, server-mediated writes)

daemon-on-user-machine        → cueapi-core   (Path 1, direct polling)
   (cue_sk_... per User)
```

The substrate accepts either; integrators pick based on the call's direction and where authn lives. **Note:** Dock is NOT a Pattern C consumer despite running both a backend and a daemon — Dock's daemon talks to Dock's app server, not cueapi-core directly. For a true hybrid, the daemon must hold a cueapi-core key.

## Common pitfalls

- **Forgetting to upsert the User row before the first Path 2 call.** Returns `user_not_found`. Solution: always `PUT /v1/internal/users/{uuid}` once per User before issuing other Path 2 requests against that UUID. Idempotent, so calling redundantly is fine.
- **Using `X-On-Behalf-Of` on Path 1 requests.** It's silently ignored. The substrate dispatches to Path 1 based on the token shape; the header is only honored when the token matches `INTERNAL_AUTH_TOKEN`. If a Path 2 call accidentally uses a Path 1 key, the request authenticates as the key's owning User regardless of header.
- **Sending the User's per-User API key INSTEAD of the internal token.** Same as above — falls through to Path 1, attribution comes from the key, the `X-On-Behalf-Of` header is ignored.
- **Rotating `INTERNAL_AUTH_TOKEN` without coordinating integrator deployment.** All in-flight Path 2 requests fail until both ends update. Plan rotation as a synchronized deployment across the substrate and the integrator service.
- **Using a per-end-user secret in `INTERNAL_AUTH_TOKEN`.** Don't. The token is shared across the integrator backend; per-user secrets belong in the integrator's authn layer, not on the substrate boundary.

## See also

- `docs/authorization-backends.md` — substrate-side authorization hook (orthogonal to this auth surface)
- `docs/agents-and-metadata.md` — agent identity model
- `docs/quickstart.md` — Path 1 default deployment walkthrough
