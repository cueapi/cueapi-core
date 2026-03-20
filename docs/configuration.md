# Configuration

CueAPI is configured entirely through environment variables. Set them in your shell, a `.env` file, or your container orchestrator.

## Required

| Variable | Description |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string. Example: `postgresql+asyncpg://user:pass@localhost:5432/cueapi` |
| `REDIS_URL` | Redis connection string. Example: `redis://localhost:6379/0` |
| `SESSION_SECRET` | Secret key for signing session tokens. Use a random string of at least 32 characters. Generate one with `openssl rand -hex 32`. |

## Optional

### Server

| Variable | Default | Description |
|---|---|---|
| `ENV` | `development` | Set to `production` to disable debug output and enable stricter defaults. |
| `ALLOW_REGISTER` | `true` | Set to `false` to disable the `/v1/auth/register` endpoint. Disable this after creating your accounts in production. |

### Poller

| Variable | Default | Description |
|---|---|---|
| `POLLER_INTERVAL_SECONDS` | `5` | How often the poller checks for cues that need to fire. Lower values reduce latency but increase database load. |
| `POLLER_BATCH_SIZE` | `500` | Maximum number of cues to process per polling cycle. Increase if you have many cues firing at the same time. |

### Webhooks

| Variable | Default | Description |
|---|---|---|
| `WEBHOOK_TIMEOUT_SECONDS` | `30` | How long to wait for a webhook endpoint to respond before marking the delivery as failed. |

### Workers

| Variable | Default | Description |
|---|---|---|
| `WORKER_HEARTBEAT_TIMEOUT_SECONDS` | `180` | If a worker has not polled in this many seconds, it is considered stale. Claimed executions from stale workers are released back to the queue. |
| `WORKER_CLAIM_TIMEOUT_SECONDS` | `900` | Maximum time (in seconds) a worker can hold a claimed execution before it is automatically released. Set this longer than your longest expected task duration. |

### Integrations

| Variable | Default | Description |
|---|---|---|
| `RESEND_API_KEY` | _(none)_ | API key for [Resend](https://resend.com) email delivery. Optional. When set, enables email notifications for failed webhook deliveries and other alerts. |

## Example `.env` file

```bash
# Required
DATABASE_URL=postgresql+asyncpg://cueapi:secret@db:5432/cueapi
REDIS_URL=redis://redis:6379/0
SESSION_SECRET=a1b2c3d4e5f6...

# Production settings
ENV=production
ALLOW_REGISTER=false

# Tuning
POLLER_INTERVAL_SECONDS=2
POLLER_BATCH_SIZE=1000
WEBHOOK_TIMEOUT_SECONDS=15
```

## Production recommendations

- **SESSION_SECRET**: Generate a unique, high-entropy secret. Never reuse it across environments. Rotating it will invalidate all active sessions.
- **Connection pooling**: For PostgreSQL, use PgBouncer or the built-in asyncpg pool. The `DATABASE_URL` connects directly; place a pooler in front for high-concurrency deployments.
- **ALLOW_REGISTER**: Always set to `false` in production after creating your accounts. Leaving it open allows anyone to create accounts on your instance.
- **WEBHOOK_TIMEOUT_SECONDS**: Keep this low (10-30s). If your endpoints need more time, use [worker transport](workers.md) instead.
