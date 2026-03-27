# Quickstart

Get CueAPI running locally in under 5 minutes.

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) (20.10+)
- [Docker Compose](https://docs.docker.com/compose/install/) (v2+)

**Python version:** 3.9-3.12 required. Python 3.13+ not yet supported. If you're on a modern Mac with Homebrew Python, run:

```bash
brew install python@3.12
python3.12 -m venv venv
source venv/bin/activate
```

## 1. Clone and start

```bash
git clone https://github.com/cueapi/cueapi-core.git
cd cueapi-core
docker compose up
```

Wait for the logs to settle. You should see:

```
cueapi-web-1     | INFO:     Uvicorn running on http://0.0.0.0:8000
cueapi-poller-1  | INFO:     Poller started
```

## 2. Verify the health endpoint

```bash
curl http://localhost:8000/health
```

Expected response:

```json
{
  "status": "healthy",
  "services": {
    "postgres": "ok",
    "redis": "ok"
  },
  "workers": 0
}
```

## 3. Register an account

```bash
curl -X POST http://localhost:8000/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "you@example.com"}'
```

Expected response:

```json
{
  "id": "usr_...",
  "email": "you@example.com",
  "api_key": "cue_sk_..."
}
```

Save the `api_key` value. You need it for all subsequent requests.

## 4. Create a cue

Create a cue that fires every hour and sends a POST to your callback URL:

```bash
curl -X POST http://localhost:8000/v1/cues \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer cue_sk_..." \
  -d '{
    "name": "hourly-ping",
    "schedule": {
      "type": "recurring",
      "cron": "0 * * * *",
      "timezone": "UTC"
    },
    "callback": {
      "url": "https://example.com/webhook"
    },
    "payload": {"message": "hello from cueapi"}
  }'
```

Expected response:

```json
{
  "id": "cue_...",
  "name": "hourly-ping",
  "status": "active",
  "transport": "webhook",
  "schedule": {
    "type": "recurring",
    "cron": "0 * * * *",
    "timezone": "UTC"
  },
  "callback": {
    "url": "https://example.com/webhook",
    "method": "POST",
    "headers": {}
  },
  "payload": {"message": "hello from cueapi"},
  "retry": {
    "max_attempts": 3,
    "backoff_minutes": [1, 5, 15]
  },
  "next_run": "2025-01-01T01:00:00Z",
  "last_run": null,
  "run_count": 0,
  "fired_count": 0,
  "created_at": "2025-01-01T00:00:00Z",
  "updated_at": "2025-01-01T00:00:00Z"
}
```

## 5. List your cues

```bash
curl http://localhost:8000/v1/cues \
  -H "Authorization: Bearer cue_sk_..."
```

Expected response:

```json
{
  "cues": [
    {
      "id": "cue_...",
      "name": "hourly-ping",
      "status": "active",
      "next_run": "2025-01-01T01:00:00Z"
    }
  ],
  "total": 1,
  "limit": 50,
  "offset": 0
}
```

## 6. Verify your cue fired

Check execution history for your cue:

```bash
curl http://localhost:8000/v1/cues/{cue_id}/executions \
  -H "Authorization: Bearer cue_sk_..."
```

You should see an execution with status `pending`, `success`, or `retrying`.

To speed this up for testing, create a cue that fires every minute:

```bash
curl -X POST http://localhost:8000/v1/cues \
  -H "Authorization: Bearer cue_sk_..." \
  -H "Content-Type: application/json" \
  -d '{
    "name": "test-every-minute",
    "schedule": {"type": "recurring", "cron": "* * * * *"},
    "callback": {"url": "https://example.com/webhook"}
  }'
```

Wait 60 seconds then check executions. You should see your first execution.

## Next steps

- Browse the interactive API docs at [http://localhost:8000/docs](http://localhost:8000/docs)
- Read [Configuration](configuration.md) to customize your deployment
- Read [Production](production.md) before going live
- Read [Workers](workers.md) if you need pull-based task delivery instead of webhooks
