# Authorization backends

cueapi-core ships a **pluggable authorization layer** for the messaging primitive (`/v1/messages` send path). Authorization is a separate concern from authentication: an authenticated request still has to clear the authorization backend before a message is created.

The default backend rejects cross-user messages — sender and recipient must share `user_id`. Self-host integrators that need cross-user messaging within their own permission model (workspace membership, organization roles, allowlists, etc.) override the backend at deployment time.

This page covers the three resolution paths, the ABC interface, the webhook wire format, fail-closed semantics, caching, and common patterns.

## Authentication vs authorization (orthogonal)

| Concern | Question | Surface |
|---|---|---|
| Authentication | "Who is this caller?" | `Authorization` header (Path 1 per-User key, or Path 2 internal token + `X-On-Behalf-Of` — see [`internal-token-auth.md`](internal-token-auth.md)) |
| Authorization | "Should this caller be allowed to message that recipient?" | `AuthorizationBackend` (this page) |

Both run on every send. Authentication establishes the principal; authorization decides whether that principal can message a specific recipient. The same backend hook runs regardless of which authentication path produced the principal.

## The three resolution paths

cueapi-core resolves the active backend at first-call (cached at module level for the process lifetime). Resolution order, first hit wins:

1. **`AUTHORIZATION_BACKEND`** env var — Python import path to a class that subclasses `AuthorizationBackend`. Imported once on first call, then cached.
2. **`AUTHZ_HOOK_URL`** env var — HTTPS URL the substrate POSTs to before accepting any message. Instantiates `WebhookAuthorizationBackend`.
3. **Default** — `SameTenantAuthorizationBackend`, which accepts only same-`user_id` sends.

If both `AUTHORIZATION_BACKEND` and `AUTHZ_HOOK_URL` are set, `AUTHORIZATION_BACKEND` wins (more direct — no network hop).

## Default behavior — `SameTenantAuthorizationBackend`

```python
class SameTenantAuthorizationBackend(AuthorizationBackend):
    async def authorize_message(self, *, sender_user_id, recipient_user_id, **_) -> bool:
        return str(sender_user_id) == str(recipient_user_id)
```

Hosted `cueapi.ai` runs this. It enforces the v1 messaging spec §3.4 same-tenant rule: agents owned by different `User` rows can't message each other. Every cross-user send returns 403 `cross_user_send_disallowed`.

Same-user sends (e.g., a User's two agents messaging each other for internal coordination) are allowed regardless of any other criterion.

If you self-host and don't need cross-user messaging, leave both env vars unset and you get this behavior.

## Path 1 — custom Python class (`AUTHORIZATION_BACKEND`)

Set `AUTHORIZATION_BACKEND=your_module.path:YourBackendClass`. The class must subclass `AuthorizationBackend` and implement `authorize_message`:

```python
from app.services.authorization_backend import AuthorizationBackend


class WorkspaceMembershipBackend(AuthorizationBackend):
    """Allow cross-user messaging when sender and recipient share a workspace."""

    async def authorize_message(
        self,
        *,
        sender_user_id: str,
        recipient_user_id: str,
        sender_agent_id: str,
        recipient_agent_id: str,
        message_kind: str = "message",
        idempotency_key: str | None = None,
    ) -> bool:
        if str(sender_user_id) == str(recipient_user_id):
            return True  # same-tenant, always allow
        # Look up workspace memberships in your own DB / cache.
        return await self._share_workspace(sender_user_id, recipient_user_id)
```

Pattern matches the existing `alert_webhook.py` plugin convention. The class is instantiated once at module load with no constructor arguments — if you need configuration, read it from environment variables in `__init__` or `authorize_message` body.

Best for: integrators that ship Python code in their cueapi-core deployment and want full control without a network hop.

## Path 2 — HTTPS hook (`AUTHZ_HOOK_URL`)

Set `AUTHZ_HOOK_URL=https://your-authz-service.example/check`. cueapi-core POSTs a signed JSON envelope to this URL on every send (with caching — see below).

Best for: integrators whose authz logic lives in a separate service or language, and integrators that don't want to embed Python code in their cueapi-core deployment. Dock's planned shape is this, with the hook backed by their workspace-membership table.

### Wire format

```http
POST {AUTHZ_HOOK_URL}
Content-Type: application/json
X-CueAPI-Timestamp: 1714678200
X-CueAPI-Signature: v1=<hex_hmac_sha256>

{
  "sender_user_id": "11111111-1111-1111-1111-111111111111",
  "recipient_user_id": "22222222-2222-2222-2222-222222222222",
  "sender_agent_id": "agt_abcdef123456",
  "recipient_agent_id": "agt_ghijkl789012",
  "message_kind": "message",
  "idempotency_key": "client-supplied-key-or-null"
}
```

Body is JSON-encoded with sorted keys. Signature payload is `"{timestamp}.{body_bytes}"` (period separator), HMAC-SHA256 with `AUTHZ_HOOK_SECRET` as the key. Verify the signature in the hook to reject forged calls.

### Expected response

```http
HTTP/1.1 200 OK
Content-Type: application/json

{"decision": "allow", "cache_ttl": 60}
```

or

```http
HTTP/1.1 200 OK
Content-Type: application/json

{"decision": "deny", "reason": "no shared workspace", "cache_ttl": 60}
```

| Field | Type | Meaning |
|---|---|---|
| `decision` | `"allow"` or `"deny"` | Required. Any other value is treated as deny. |
| `cache_ttl` | int (seconds) | Optional. Substrate caches the decision for this many seconds keyed on `(sender_user_id, recipient_user_id, message_kind)`. Default 60. Set to 0 to disable caching for this decision. |
| `reason` | string | Optional. Logged on deny; never returned to the sender. |

The hook MUST respond within 5 seconds. Timeout = deny.

### Fail-closed semantics

Anything other than a 200 with valid `decision: "allow"` is a deny:

| Hook response | Substrate decision |
|---|---|
| 200 OK + `{"decision": "allow"}` | allow |
| 200 OK + `{"decision": "deny", ...}` | deny |
| 200 OK + invalid / non-JSON body | deny + log |
| 200 OK + `decision` is anything else | deny + log |
| Non-200 status | deny + log |
| Connection refused / DNS failure / TLS error | deny + log |
| Timeout (>5s) | deny + log |

Fail-closed is intentional. A flaky authz hook blocks legitimate traffic; a fail-open semantic would allow forged requests during a hook outage. Operate the hook with reliability in mind.

### Signing the hook secret

Generate `AUTHZ_HOOK_SECRET` with strong entropy:

```sh
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

If `AUTHZ_HOOK_SECRET` is empty, the substrate omits the `X-CueAPI-Signature` header entirely. Don't run a production hook without a secret — anyone who can reach the hook URL would otherwise be able to send forged authz requests. The hook should reject any call missing or failing signature verification.

## Caching

When the hook returns `cache_ttl > 0`, the substrate stores the decision in Redis at `authz:{sender_user_id}:{recipient_user_id}:{message_kind}` with that TTL. Subsequent sends matching the same triple short-circuit the hook call.

Cache invalidation in cueapi-core is TTL-only — there's no purge endpoint. If a permission change in the integrator's data store needs to take effect immediately, set `cache_ttl: 0` on every response (forces a hook call per send) or use a short TTL like 5 seconds. The default 60s is a balance between latency on hot conversations and freshness on permission changes.

If Redis is unavailable when the substrate tries to read the cache, the lookup fails open and the hook is called. Redis unavailability for the **write** silently drops the cache update; the next send re-calls the hook. Neither failure mode produces an authorization bypass.

## The `AuthorizationBackend` interface

```python
class AuthorizationBackend(ABC):
    @abstractmethod
    async def authorize_message(
        self,
        *,
        sender_user_id: str,
        recipient_user_id: str,
        sender_agent_id: str,
        recipient_agent_id: str,
        message_kind: str = "message",
        idempotency_key: Optional[str] = None,
    ) -> bool:
        """Return True if the message should be accepted, False otherwise."""
```

All arguments are keyword-only. The decision is binary; any contextual reasoning belongs in the integrator's logs (or in the `reason` field of the webhook response), not in the return value.

| Argument | Notes |
|---|---|
| `sender_user_id` | UUID string of the User row that owns the sender agent. |
| `recipient_user_id` | UUID string of the User row that owns the recipient agent. Equal to `sender_user_id` for same-tenant messages. |
| `sender_agent_id` | Opaque `agt_xxx` ID. |
| `recipient_agent_id` | Opaque `agt_xxx` ID. |
| `message_kind` | Currently always `"message"`. Reserved for future kinds (replies, broadcasts, ack-only); backends should accept new values without crashing. |
| `idempotency_key` | The client-supplied idempotency key, or `None`. Passed through for backends that want to log it; cueapi-core handles dedup itself. |

## Common patterns

### Allowlist (static)

A pre-computed pair-wise allowlist read from a config file or env var:

```python
ALLOWED = {
    ("11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"),
    # ...
}


class AllowlistBackend(AuthorizationBackend):
    async def authorize_message(self, *, sender_user_id, recipient_user_id, **_) -> bool:
        if str(sender_user_id) == str(recipient_user_id):
            return True
        key = (str(sender_user_id), str(recipient_user_id))
        return key in ALLOWED
```

Suitable for small, slow-changing pair-wise relationships (e.g., system-to-system bridges).

### Everyone (open)

Every authenticated send is authorized. Use only in trusted single-tenant environments where authentication itself is the access control:

```python
class EveryoneBackend(AuthorizationBackend):
    async def authorize_message(self, **_) -> bool:
        return True
```

Don't run this on a public deployment. Authentication being correct is necessary but not sufficient — without authorization, any compromised key can message any agent.

### Workspace membership (Dock-shape)

The integrator owns a "workspace memberships" table (or equivalent permission model). The backend looks up sender + recipient and returns true if they share at least one workspace:

```python
class WorkspaceMembershipBackend(AuthorizationBackend):
    async def authorize_message(self, *, sender_user_id, recipient_user_id, **_) -> bool:
        if str(sender_user_id) == str(recipient_user_id):
            return True
        sender_ws = await self._workspaces_for(sender_user_id)
        recipient_ws = await self._workspaces_for(recipient_user_id)
        return bool(sender_ws & recipient_ws)
```

The `_workspaces_for` lookup typically hits the integrator's DB or a Redis-backed memo. Cache aggressively — most sends are within a hot conversation that resolves the same authorization repeatedly.

If the lookup is in a different service or language, use the webhook backend instead.

### Per-pair role check (granular)

Same shape as workspace membership, but the predicate is "sender's role in the shared scope is ≥ X" rather than "shared scope exists":

```python
class RoleGatedBackend(AuthorizationBackend):
    async def authorize_message(self, *, sender_user_id, recipient_user_id, **_) -> bool:
        if str(sender_user_id) == str(recipient_user_id):
            return True
        return await self._sender_can_dm(sender_user_id, recipient_user_id)
```

The complexity of the role check belongs in `_sender_can_dm`, not in the backend wrapper. Keep the backend itself thin.

## Common pitfalls

- **Forgetting to allow same-user sends.** If your custom backend doesn't short-circuit on `sender_user_id == recipient_user_id`, you'll block agents owned by the same user from messaging each other. Most patterns above include the short-circuit explicitly.
- **Returning anything other than `True`/`False`.** The interface is strict. Don't return `1`, `"allow"`, `None`, etc. — the type signature is `bool` and the substrate doesn't coerce.
- **Slow hook responses.** The 5-second timeout is a hard ceiling. Hooks slower than 1-2 seconds will produce noticeable user-side latency on send. Cache aggressively in the hook itself.
- **Setting `cache_ttl` too high during a permission migration.** When you change permissions, in-flight cached "allow" decisions persist for `cache_ttl` seconds. During cutover, return `cache_ttl: 0` until the migration settles, then bump back to 60.
- **Mixing `AUTHORIZATION_BACKEND` and `AUTHZ_HOOK_URL`.** Only one runs (the import-path backend wins). Pick one shape per deployment; setting both invites confusion when a config change to the unused one mysteriously has no effect.
- **Forgetting to verify signatures in the webhook hook.** Without verification, anyone who can reach the hook URL can submit forged authz checks. Always verify `X-CueAPI-Signature` against `AUTHZ_HOOK_SECRET` before trusting the body.

## Configuration reference

| Env var | Default | Purpose |
|---|---|---|
| `AUTHORIZATION_BACKEND` | unset | Python import path `module.path:ClassName`. Imported once on first call, cached for process lifetime. Wins over `AUTHZ_HOOK_URL` if both set. |
| `AUTHZ_HOOK_URL` | unset | HTTPS URL the substrate POSTs to. Used when `AUTHORIZATION_BACKEND` is unset. |
| `AUTHZ_HOOK_SECRET` | `""` | HMAC-SHA256 key for signing the webhook body. Empty disables signing — don't run production without a secret. |

Restart the substrate process to pick up new config; the resolved backend is cached at module level.

## See also

- [`internal-token-auth.md`](internal-token-auth.md) — the authentication surface (orthogonal to this one)
- [`agents-and-metadata.md`](agents-and-metadata.md) — agent identity model
- `app/services/authorization_backend.py` — implementation source
