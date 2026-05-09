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

- Server-side integrator backends use when proxying multiple end-users to the substrate (Dock's `mirrorAgentToCue`, an enterprise app server forwarding employee actions, etc.).
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
| Mixed: backend uses Path 2, client daemons use Path 1 (Dock pattern) | Both |

It's common for a single deployment to use **both paths simultaneously**. Dock, for example, uses Path 2 for its server-side message creation (`mirrorAgentToCue` posting to `/v1/agents` on behalf of any Dock-User) and Path 1 for its `dock-live` daemon that polls a single User's inbox per machine. The substrate accepts either; the choice is per-request.

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

Use the internal upsert endpoint:

```
PUT /v1/internal/users/{user-uuid}
Authorization: Bearer <INTERNAL_AUTH_TOKEN>
Content-Type: application/json

{
  "email": "user@integrator.example",
  "slug": "integrator-user-slug"
}
```

Idempotent — calling repeatedly with the same UUID + body is a no-op (returns the existing row). Required exactly once per User the integrator wants to act on behalf of; subsequent message/agent operations under Path 2 don't re-call this endpoint.

Generated User rows have a stub API key (NOT NULL constraint satisfaction) but it is never returned via this endpoint — Path 2 callers don't need it. If the integrator ever wants the User to have a usable Path 1 API key, regenerate via `POST /v1/auth/key/regenerate` after the upsert.

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

The integrator runs a backend service. End-users authenticate to the backend (OAuth / session tokens / whatever). The backend proxies a subset of cueapi operations to cueapi-core using Path 2.

```
end-user → integrator backend → cueapi-core
            (OAuth)             (INTERNAL_AUTH_TOKEN
                                 + X-On-Behalf-Of: <user-uuid>)
```

Best for: chat products, workspace tools, multi-tenant SaaS where the integrator owns the user-database and identity model.

### Pattern B — daemon-style poller (dock-live-shape)

Same end-user, on their own machine, runs a long-running daemon (Go binary, Python script, etc.) that polls the substrate directly using a per-User Path 1 key.

```
daemon-on-user-machine → cueapi-core
   (cue_sk_... per User)
```

Best for: per-machine push delivery, presence tracking, single-User scope. Avoids server-side proxy hop on the polling path.

### Pattern C — hybrid (Dock's actual setup)

Most production integrators use both. Server-side actions (creating agents, sending on behalf of users) flow through Path 2; per-machine daemons use Path 1.

```
end-user → integrator backend → cueapi-core   (Path 2, server-mediated writes)
       ↓
daemon-on-user-machine        → cueapi-core   (Path 1, direct polling)
```

The substrate accepts either; integrators pick based on the call's direction and where authn lives.

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
