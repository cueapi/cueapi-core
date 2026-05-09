# Agents and metadata

Agents are the addressable identities in CueAPI's messaging primitive. Each agent row carries an opaque JSON `metadata` field that consumers use to attach product-specific information without touching the substrate.

This page covers the metadata model, the well-known-keys convention, the consumer-namespacing pattern, and the `agent_metadata` DB-column workaround you may encounter in source.

## Why metadata is opaque

CueAPI Core stays narrow about what an agent *is*. The substrate routes messages by ID, manages presence, and enforces tenant boundaries — it does not categorize agents.

Categorization is a consumer concern. Different products have different taxonomies:

- A workspace product may distinguish humans from agents from synthetic "self" identities.
- A native developer tool may distinguish runtime types (Claude Code, Cowork, OpenClaw).
- A CI/CD integration may distinguish bot roles (PR-reviewer, build-runner, smoke-tester).

Forcing one of these taxonomies into the substrate would be wrong for the others. So `agents.metadata` is a JSONB column the substrate writes through but never interprets. Consumers stamp whatever shape fits their product; the substrate stays out of the way.

## The metadata field

### API surface

`agents.metadata` is exposed as the `metadata` field on every agent endpoint:

| Endpoint | Field |
|---|---|
| `POST /v1/agents` | Accepts `metadata` in request body. Defaults to `{}`. |
| `GET /v1/agents/{ref}` | Returns `metadata` in response. |
| `GET /v1/agents` | Returns `metadata` on each agent in the list. |
| `PATCH /v1/agents/{ref}` | Accepts `metadata`. **Whole-replace**, not deep-merge. See [Update semantics](#update-semantics). |

### Example: creating an agent with metadata

```bash
curl -X POST http://localhost:8000/v1/agents \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer cue_sk_..." \
  -d '{
    "slug": "scout",
    "display_name": "Scout",
    "metadata": {
      "kind": "agent",
      "version": "1.0",
      "myproduct.team": "growth"
    }
  }'
```

Response:

```json
{
  "id": "agt_abc123def456",
  "slug": "scout",
  "display_name": "Scout",
  "metadata": {
    "kind": "agent",
    "version": "1.0",
    "myproduct.team": "growth"
  },
  "status": "online",
  "...": "..."
}
```

### Update semantics

`PATCH /v1/agents/{ref}` with a `metadata` field **replaces the entire object**. There is no deep-merge. To preserve existing keys, read first, modify, then PATCH the full object.

```bash
# Read current metadata.
metadata=$(curl -s http://localhost:8000/v1/agents/agt_abc123def456 \
  -H "Authorization: Bearer cue_sk_..." \
  | jq '.metadata')

# Add a key, keeping the rest.
new_metadata=$(echo "$metadata" | jq '. + {"myproduct.last_active": "2026-05-08T12:00:00Z"}')

# PATCH with the full merged object.
curl -X PATCH http://localhost:8000/v1/agents/agt_abc123def456 \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer cue_sk_..." \
  -d "{\"metadata\": $new_metadata}"
```

To clear metadata entirely, pass `{}`:

```bash
curl -X PATCH http://localhost:8000/v1/agents/agt_abc123def456 \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer cue_sk_..." \
  -d '{"metadata": {}}'
```

Omitting `metadata` from the PATCH body leaves it unchanged.

## Well-known keys

CueAPI ships a small registry of recommended top-level keys. Use these for cross-consumer interoperability — every consumer reads them the same way.

| Key | Type | Description |
|---|---|---|
| `kind` | string | Coarse classification of what this agent is. Conventional values: `agent`, `human`, `system`, `self`. Consumers may use product-specific values (e.g. `claude_code`, `cowork`); see [Consumer-namespacing](#consumer-namespacing) for product-specific extensions. |
| `version` | string | Schema version of the consumer's metadata payload. Useful when the consumer evolves its schema and wants to detect old rows. |

The substrate does not validate well-known-key values. Consumers can stamp anything; it is convention, not enforcement.

### Choosing values for `kind`

There is no canonical enum. Pick what makes sense for your product, document it in your own surface, and stay consistent. Common patterns:

- **Workspace tools:** `human`, `agent`, `system` (where `system` covers built-in bots that aren't user-owned).
- **Native developer tools:** values that name the runtime — `claude_code`, `cowork`, `openclaw`.
- **CI/CD tools:** values that name the role — `pr_reviewer`, `build_runner`, `smoke_tester`.

If you are building a new consumer, start with the four conventional values (`agent`, `human`, `system`, `self`) and only specialize when you have a real reason.

## Consumer-namespacing

Anything that is specific to your product belongs under a namespaced key. Use a short consumer prefix on the key:

```json
{
  "kind": "agent",
  "version": "1.0",
  "dock.workspace_count": 7,
  "dock.last_workspace": "engineering"
}
```

Conventions:

- **Pick a short, lowercase prefix** for your product. Two to four characters is typical (`dock`, `cma`, `obs`).
- **Use a dot separator.** `dock.team`, not `dock_team` or `dockTeam`. Easier to grep and clear about the namespace.
- **Substrate ignores unknown keys.** It will not reject novel namespaces; you do not need to register your prefix anywhere.
- **Cross-consumer alignment is not required.** Two consumers picking the same prefix is unlikely (the prefix is a free choice in your own product), and the substrate would not enforce it anyway. If you ship a public consumer and want to reduce collision risk, document your prefix in your own user-facing docs.

The substrate is **aware-by-convention, not aware-by-validation**: it knows about `kind` and `version` because the docs say so, not because it parses or validates them. Consumers extend the convention by namespace.

## Worked examples

### Example 1: Workspace product (Dock-shape)

A workspace product distinguishes humans, named agents, and a synthetic per-user "self" identity:

```json
{
  "kind": "self",
  "dock.origin": "synthetic",
  "dock.workspace_id": "ws_abc123"
}
```

```json
{
  "kind": "agent",
  "dock.owner_user_id": "usr_xyz789",
  "dock.created_via": "ui"
}
```

```json
{
  "kind": "human",
  "dock.user_id": "usr_xyz789"
}
```

### Example 2: Native developer tool

A developer tool that supervises multiple agent runtimes per machine, each with a routing-mode setting orthogonal to the runtime type:

```json
{
  "kind": "claude_code",
  "version": "1.0",
  "cma.routing_mode": "live_or_background",
  "cma.instance_slug": "myproject",
  "cma.machine": "laptop-mike"
}
```

`kind` records the runtime; `cma.routing_mode` records routing behavior the runtime can flip without changing kind. Keeping these orthogonal preserves the per-agent distinction between runtime type and per-agent settings.

### Example 3: Observability tool

```json
{
  "kind": "system",
  "version": "1.2",
  "obs.role": "log_tap",
  "obs.environment": "production"
}
```

## The `agent_metadata` column-name workaround

If you read the source code or write SQL against the database, you will see the column is named `agent_metadata` rather than `metadata`. This is a SQLAlchemy compatibility workaround.

`metadata` is a reserved attribute on SQLAlchemy's declarative `Base` class. Using it as a column name would shadow the `Base.metadata` registry. The ORM model maps a trailing-underscore Python attribute to a renamed DB column:

```python
class Agent(Base):
    __tablename__ = "agents"

    # ...

    metadata_ = Column(
        "agent_metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )
```

What you see at each layer:

| Layer | Name |
|---|---|
| API request/response (Pydantic schemas) | `metadata` |
| ORM attribute (Python code) | `metadata_` (trailing underscore) |
| Database column | `agent_metadata` |

If you query the database directly:

```sql
SELECT id, slug, agent_metadata FROM agents WHERE slug = 'scout';
```

If you write Python against the ORM:

```python
agent.metadata_["kind"] = "claude_code"
```

If you call the HTTP API, use `metadata`:

```bash
curl -X PATCH http://localhost:8000/v1/agents/agt_abc \
  -d '{"metadata": {"kind": "claude_code"}}'
```

The Pydantic schema layer translates between API field name and ORM attribute. Consumers of the HTTP API never need to know about `metadata_` or `agent_metadata`.

## Slug uniqueness and address resolution

Agent slugs are unique **per user**, enforced by `UniqueConstraint("user_id", "slug")`. Cross-user slug collision is impossible by design.

Three address shapes:

| Shape | Example | Resolution |
|---|---|---|
| Opaque ID | `agt_abc123def456` | Direct lookup. Canonical for substrate operations. |
| Slug-form (with user) | `scout@govind` | Resolves via `User.slug == "govind"` AND `Agent.slug == "scout"`. |
| Synthetic-self (consumer convention) | `self@govind` | A regular agent with `slug = "self"` under user `govind`. The substrate does not know it represents "the user themselves" — that is consumer convention; the consumer typically stamps `metadata.kind = "self"`. |

The opaque ID (`agt_xxx`) is canonical. Slug-form addresses are translated to opaque IDs before any substrate-level operation. Cross-system routing, audit logs, and references in code should use the opaque form.

## What metadata is not

Things that should **not** live in `agents.metadata`:

- **Authentication credentials.** Use API keys (`POST /v1/auth/key/regenerate`) or webhook secrets (per-agent `webhook_secret` field).
- **Webhook URL or secret.** These are first-class fields on the agent row (`webhook_url`, `webhook_secret`).
- **Presence state.** Use `agent.status` (`online` / `offline` / `away`) — substrate-managed.
- **Per-message or per-cue context.** Those belong on the message or cue payload, not on the agent.
- **Large blobs.** The column is JSONB; nothing technically prevents storing megabytes of data. But it is fetched on every agent read and copied into every webhook payload that includes the recipient agent. Keep it small (kilobytes, not megabytes). If you need to attach large data to an agent, store it in your own product database keyed by `agent.id`.

## Reading metadata across a multi-agent fleet

`GET /v1/agents` returns metadata on every agent in the list. Pagination applies:

```bash
curl http://localhost:8000/v1/agents?limit=100&offset=0 \
  -H "Authorization: Bearer cue_sk_..."
```

```json
{
  "agents": [
    {
      "id": "agt_abc",
      "slug": "scout",
      "metadata": {"kind": "agent", "version": "1.0"},
      "...": "..."
    },
    {
      "id": "agt_xyz",
      "slug": "self",
      "metadata": {"kind": "self"},
      "...": "..."
    }
  ],
  "total": 2,
  "limit": 100,
  "offset": 0
}
```

If you only need a few keys for rendering, do not re-fetch each agent — keep the metadata you already have from the list response.

## See also

- [Authorization backends](authorization-backends.md) — how the substrate gates messaging between agents (separate concern from metadata).
- [Quickstart](quickstart.md) — registering your first agent.
- [Workers](workers.md) — pull-based delivery; uses agent identities for routing.
