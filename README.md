# CueAPI

![Python 3.9-3.12](https://img.shields.io/badge/python-3.9--3.12-blue)

Scheduling infrastructure for AI agents. Open source core, hosted at [cueapi.ai](https://cueapi.ai).

## The problem

Cron fires jobs. That is all it does.

It has no concept of whether your job succeeded. No retries when it fails.
No execution history. No outcome reporting. No alerts when something goes wrong.

When your agent runs at 3am and silently fails, cron does not know. Neither do you.

## What CueAPI does differently

- **Execution proof** — every execution tracked with delivery status and outcome separately
- **Automatic retries** — exponential backoff (1, 5, 15 min) when delivery fails
- **Outcome reporting** — your handler reports success or failure explicitly
- **Worker transport** — no public URL needed, worker daemon polls for jobs
- **Webhook delivery** — signed payloads delivered to your handler
- **Failure alerts** — email and webhook when all retries exhaust

## What CueAPI is not

- Not a workflow orchestrator
- Not a DAG engine
- Not an agent runtime
- Not a replacement for task queues like Celery or RQ

CueAPI does one thing: schedules tasks and tells you whether they succeeded.

## Quick start

Prerequisites:

- Docker and Docker Compose
- Python 3.9-3.12 (Python 3.13+ not yet supported — asyncpg dependency)
- Git

```bash
git clone https://github.com/govindkavaturi-art/cueapi-core
cd cueapi-core
docker compose up
```

CueAPI is running at http://localhost:8000

Create your first cue:

```bash
# Register
curl -X POST http://localhost:8000/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "you@example.com"}'

# Create a cue
curl -X POST http://localhost:8000/v1/cues \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "morning-brief",
    "schedule": {
      "type": "recurring",
      "cron": "0 9 * * *",
      "timezone": "America/Los_Angeles"
    },
    "callback": {
      "url": "https://example.com/webhook"
    }
  }'
```

## Architecture

CueAPI uses a transactional outbox pattern for at-least-once delivery.

```
Client → API → PostgreSQL (cue + outbox in same transaction)
                    ↓
              Poller (every 30s)
                    ↓
              Outbox Dispatcher
                    ↓
         Webhook Handler / Worker Daemon
                    ↓
              Outcome Reporting
```

Key design decisions:

- **Transactional outbox** — cue creation and delivery scheduling are atomic
- **Claim race condition** — solved via PostgreSQL SELECT FOR UPDATE SKIP LOCKED
- **At-least-once delivery** — guaranteed, handlers should be idempotent
- **Worker pull model** — no public URL needed, worker polls for available jobs

## Self-hosting

Full self-hosting documentation:

- [Quick start](docs/quickstart.md) — clone, run, create your first cue
- [Configuration](docs/configuration.md) — every environment variable documented
- [Workers](docs/workers.md) — worker transport setup and scaling
- [Production](docs/production.md) — PostgreSQL, Redis, reverse proxy, monitoring
- [FAQ](docs/faq.md) — common self-hosting questions

## Hosted service

Don't want to manage the infrastructure? [cueapi.ai](https://cueapi.ai) is the hosted version.

Free tier: 10 cues, 300 executions/month.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Apache 2.0
