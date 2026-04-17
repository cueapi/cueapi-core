<!-- Pipeline validation: 2026-04-04 -->
> **For AI agents:** This is the CueAPI server. Run it with `git clone https://github.com/cueapi/cueapi-core && cd cueapi-core && docker compose up`. Base URL: `http://localhost:8000`. Register: `POST /v1/auth/register` with `{"email": "you@example.com"}` -- API key is returned directly in the response. No email required. All requests need `Authorization: Bearer YOUR_API_KEY`. Create a cue: `POST /v1/cues` with `{"name": "task-name", "schedule": {"type": "recurring", "cron": "0 9 * * *"}, "callback": {"url": "https://your-handler.com"}}`. Check executions: `GET /v1/executions`. Report outcome from your handler by responding with `{"success": true}` or `{"success": false, "error": "reason"}`. Retries happen automatically at 1, 5, and 15 minutes on failure.

# CueAPI

**Your agents are failing silently. CueAPI tells you when and why.**

*Cron has no concept of success. Cue does.*

[![Version](https://img.shields.io/badge/version-0.1.0-black)](CHANGELOG.md)
[![Python 3.9-3.12](https://img.shields.io/badge/python-3.9--3.12-blue)](https://www.python.org)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-green)](LICENSE)
[![Tests: 367](https://img.shields.io/badge/tests-367-brightgreen)](https://github.com/cueapi/cueapi-core)
[![Docker](https://img.shields.io/badge/docker-compose%20up-blue)](https://github.com/cueapi/cueapi-core)

The open source server for CueAPI. Run it yourself with Docker. Hosted at [cueapi.ai](https://cueapi.ai).

## Open core model

cueapi-core is the scheduling + delivery + outcome-tracking engine. Hosted cueapi.ai adds a dashboard, managed email alerts, billing, and a few other SaaS-business-layer features — see [HOSTED_ONLY.md](HOSTED_ONLY.md) for the full list and reasoning. Nothing in the OSS scheduler is crippled; what's here is what runs in production.

If you want a hosted-only feature ported to OSS, [open an issue](https://github.com/cueapi/cueapi-core/issues/new) — see the "Contributing a port" section in [HOSTED_ONLY.md](HOSTED_ONLY.md).

---

## The problem with cron

Cron fires jobs. That is all it does.

It has no concept of whether your job succeeded. No retries when it fails. No execution history. No outcome reporting. No alerts when something goes wrong.

When your agent runs at 3am and silently fails, cron does not know. Neither do you.

```bash
# This is what your agent sees with cron:
0 3 * * * python run_agent.py

# Did it run? Who knows.
# Did it succeed? No idea.
# Did it retry on failure? No.
# Are you alerted? Never.
```

## See it in action

<img src="https://raw.githubusercontent.com/cueapi/cueapi-core/main/docs/execution-feed.gif" width="100%" alt="CueAPI Live Execution Feed" />

## What CueAPI does differently

```json
{
  "execution_id": "exec_01HX...",
  "status": "success",
  "outcome_success": true,
  "attempts": 1,
  "next_run": "2026-03-28T03:00:00Z",
  "delivered_at": "2026-03-28T03:00:04Z"
}
```

| Feature | Cron | CueAPI |
|---------|------|--------|
| Fires the job | Yes | Yes |
| Knows if it succeeded | No | Yes |
| Retries on failure | No | Yes (exponential backoff) |
| Execution history | No | Yes |
| Alerts on exhausted retries | No | Yes (email + webhook) |
| Works without a public URL | No | Yes (worker transport) |

## Quick start

**Prerequisites:** Docker, Docker Compose, Git

```bash
git clone https://github.com/cueapi/cueapi-core
cd cueapi-core
docker compose up
```

CueAPI is running at `http://localhost:8000`.

### Create your first cue

```bash
# 1. Register
curl -X POST http://localhost:8000/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "you@example.com"}'

# Your API key is returned directly in the response.

# 2. Schedule an agent task
curl -X POST http://localhost:8000/v1/cues \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "morning-agent-brief",
    "schedule": {
      "type": "recurring",
      "cron": "0 9 * * *",
      "timezone": "America/Los_Angeles"
    },
    "callback": {
      "url": "https://your-agent.com/run"
    },
    "payload": {
      "task": "daily_brief"
    }
  }'

# 3. Check execution history
curl http://localhost:8000/v1/executions \
  -H "Authorization: Bearer YOUR_API_KEY"
```

## Two transport modes

### Webhook (default)

CueAPI POSTs a signed payload to your handler URL when a cue fires. Your handler reports success or failure.

```
CueAPI -> POST /your-webhook -> Your handler reports outcome -> CueAPI records result
```

### Worker (no public URL needed)

Your daemon polls CueAPI for executions. Perfect for agents running locally, behind firewalls, or in private networks.

```
CueAPI <- Worker polls -> Execution delivered -> Worker reports outcome -> CueAPI records result
```

See [cueapi-worker](https://pypi.org/project/cueapi-worker/) for the worker daemon.

## Architecture

CueAPI uses a transactional outbox pattern for at-least-once delivery.

```
Client -> API -> PostgreSQL (cue + outbox in same transaction)
                    |
              Poller (every 30s)
                    |
              Outbox Dispatcher
                    |
         Webhook Handler / Worker Daemon
                    |
              Outcome Reporting -> Execution record updated
                    |
         (on failure) Retry with exponential backoff (1, 5, 15 min)
                    |
         (retries exhausted) Email + webhook alert fired
```

**Key design decisions:**

- **Transactional outbox** -- cue creation and delivery scheduling are atomic. No job is ever lost.
- **SELECT FOR UPDATE SKIP LOCKED** -- claim races solved at the database level, no distributed locking needed.
- **At-least-once delivery** -- guaranteed. Handlers should be idempotent.
- **Worker pull model** -- no inbound firewall rules, no ngrok, no public URL required.
- **Outcome separation** -- delivery status (did we reach your handler?) and outcome (did your handler succeed?) are tracked independently.

## API reference

26 endpoints across 8 resource groups.

| Resource | Endpoints |
|----------|-----------|
| Auth | Register, magic link, refresh, device code flow |
| Cues | Create, list, get, update, delete, pause, resume |
| Executions | List, get, report outcome |
| Workers | Register worker, poll for jobs |
| Webhook secrets | Create, rotate |
| Usage | Get current usage and limits |
| Health | Liveness, readiness |

Full reference: [docs.cueapi.ai/api](https://docs.cueapi.ai/api-reference/overview)

### Pause and resume a cue
```bash
# Pause
curl -X PATCH http://localhost:8000/v1/cues/CUE_ID \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"status": "paused"}'

# Resume
curl -X PATCH http://localhost:8000/v1/cues/CUE_ID \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"status": "active"}'
```

### Worker heartbeat

Workers register and claim executions via the heartbeat endpoint:
```bash
curl -X POST http://localhost:8000/v1/worker/heartbeat \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"handlers": ["task-name-1", "task-name-2"]}'
```

The handlers array tells CueAPI which cue names this worker can process.

### Verification modes

Cues can require evidence on the outcome report. Configure a `verification` policy at create or update time:

```bash
curl -X POST http://localhost:8000/v1/cues \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{"name": "nightly-report", "schedule": {"type": "recurring", "cron": "0 9 * * *"},
       "callback": {"url": "https://your-handler.com"},
       "verification": {"mode": "require_external_id"}}'
```

Five modes:

| Mode | Behavior |
|------|----------|
| `none` (default) | Reported `success` is final — `reported_success` / `reported_failure`. |
| `require_external_id` | Outcome must include `external_id`. Missing → `verification_failed`. Present → `verified_success`. |
| `require_result_url` | Outcome must include `result_url`. |
| `require_artifacts` | Outcome must include `artifacts` (non-empty). |
| `manual` | Every successful outcome parks in `verification_pending` until someone calls `POST /v1/executions/{id}/verify`. |

Report outcomes with evidence inline on the existing endpoint:

```bash
curl -X POST http://localhost:8000/v1/executions/EXEC_ID/outcome \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{"success": true, "external_id": "stripe_ch_abc123",
       "result_url": "https://dashboard.stripe.com/payments/ch_abc123",
       "summary": "Charged customer 42"}'
```

Manually verify or reject a parked outcome:

```bash
# Approve
curl -X POST http://localhost:8000/v1/executions/EXEC_ID/verify \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{"valid": true}'

# Reject (e.g. after audit)
curl -X POST http://localhost:8000/v1/executions/EXEC_ID/verify \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{"valid": false, "reason": "invoice number does not match"}'
```

Backward-compat paths still work: `POST /outcome` with just `{success: true}` behaves identically to before, and `PATCH /v1/executions/{id}/evidence` remains available as a two-step alternative.

> Worker-transport cues can currently use `none` or `manual` only. Evidence-requiring modes (`require_external_id`, `require_result_url`, `require_artifacts`) are rejected at create/update time with `400 unsupported_verification_for_transport`. This restriction will be lifted once cueapi-worker 0.3.0 ships to PyPI with evidence reporting via `CUEAPI_OUTCOME_FILE`.

## Alerts

cueapi-core persists alerts when outcomes go wrong and — if you configure a webhook URL — POSTs them to you with an HMAC signature. Three alert types today:

| Type | When it fires |
|------|--------------|
| `consecutive_failures` | Same cue reports `success=false` three runs in a row |
| `verification_failed` | Outcome is missing evidence required by the cue's verification mode (see "Verification modes") |
| `outcome_timeout` | Handler never reports an outcome before the deadline (not yet wired in OSS — coming with the deadline-checking poller) |

Alerts are deduplicated per `(user, alert_type, execution_id)` within a 5-minute window so flapping executions don't flood your inbox.

### Query alerts directly

```bash
curl http://localhost:8000/v1/alerts \
  -H "Authorization: Bearer YOUR_API_KEY"
# Optional filters: ?alert_type=consecutive_failures&since=2026-01-01T00:00:00Z&limit=20
```

### Receive alerts via webhook

1. **Set your webhook URL:**
   ```bash
   curl -X PATCH http://localhost:8000/v1/auth/me \
     -H "Authorization: Bearer YOUR_API_KEY" \
     -d '{"alert_webhook_url": "https://your-server.example.com/cueapi-alerts"}'
   ```

2. **Retrieve your signing secret** (generated lazily on first call, 64 hex chars):
   ```bash
   curl http://localhost:8000/v1/auth/alert-webhook-secret \
     -H "Authorization: Bearer YOUR_API_KEY"
   ```
   Rotate with `POST /v1/auth/alert-webhook-secret/regenerate` (requires `X-Confirm-Destructive: true`).

3. **Verify incoming alerts** on your end. See [`examples/alert_webhook_receiver.py`](examples/alert_webhook_receiver.py) for a 30-line Flask receiver. Each POST carries `X-CueAPI-Signature: v1=<hex>`, `X-CueAPI-Timestamp`, `X-CueAPI-Alert-Id`, and `X-CueAPI-Alert-Type`.

> **Delivery path is HTTP only.** cueapi-core ships alert persistence + webhook delivery and nothing else. For email / SMS / Slack, point your `alert_webhook_url` at a forwarder you control, or use hosted cueapi.ai which includes managed email delivery via SendGrid. See [HOSTED_ONLY.md](HOSTED_ONLY.md) for the full open-core policy.

## What CueAPI is not

- Not a workflow orchestrator
- Not a DAG engine
- Not an agent runtime
- Not a replacement for task queues like Celery or RQ

CueAPI does one thing: schedules tasks and tells you whether they succeeded.

## Self-hosting

| Guide | Description |
|-------|-------------|
| [Quick start](docs/quickstart.md) | Clone, configure, run |
| [Configuration](docs/configuration.md) | Every environment variable documented |
| [Workers](docs/workers.md) | Worker transport setup and scaling |
| [Production](docs/production.md) | PostgreSQL, Redis, reverse proxy, monitoring |
| [FAQ](docs/faq.md) | Common self-hosting questions |

**Stack:** FastAPI, PostgreSQL 16, Redis 7, Docker

## Hosted service

Don't want to manage the infrastructure? [cueapi.ai](https://cueapi.ai) is the hosted version.

**Free tier:** 10 cues, 300 executions/month. No credit card required.

[Get started](https://dashboard.cueapi.ai/signup)

## SDKs

- [cueapi-python](https://github.com/cueapi/cueapi-python) -- Official Python SDK (`pip install cueapi-sdk`)

## Contributing

CueAPI is open source under Apache 2.0. Contributions welcome.

```bash
git clone https://github.com/cueapi/cueapi-core
cd cueapi-core
docker compose up
```

The test suite has 367 tests run by Argus, our QA agent. All PRs must pass.

Good first issues are labeled `good first issue` on GitHub.

See [CONTRIBUTING.md](CONTRIBUTING.md) for full contribution guidelines.

## Security

CueAPI was independently security-audited with 50+ penetration tests covering SSRF bypass (19 vectors), authentication and authorization, input validation, rate limiting, and information disclosure. Automated scanning performed with OWASP ZAP and Nuclei.

- API keys hashed with SHA-256 - never stored in plaintext
- SSRF protection with dual-time DNS validation (creation + delivery)
- Stripe-style webhook signing (HMAC-SHA256 with replay protection)
- Sliding-window rate limiting per API key and per IP
- Zero sensitive data in logs or error responses

See [SECURITY.md](SECURITY.md) for responsible disclosure, architecture details, and self-hosting hardening guide.

## License

[Apache 2.0](LICENSE). See [LICENSE](LICENSE).

[Changelog](CHANGELOG.md)

---

Built by [Vector Apps](https://cueapi.ai/about) · Hosted at [cueapi.ai](https://cueapi.ai) · Docs at [docs.cueapi.ai](https://docs.cueapi.ai) · [Changelog](CHANGELOG.md)
