# Quickstart

Get CueAPI running locally in under 5 minutes.

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) (20.10+)
- [Docker Compose](https://docs.docker.com/compose/install/) (v2+)

## 1. Clone and start

```bash
git clone https://github.com/govindkavaturi-art/cueapi-core.git
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
{"status": "ok"}
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

## Next steps

- Browse the interactive API docs at [http://localhost:8000/docs](http://localhost:8000/docs)
- Read [Configuration](configuration.md) to customize your deployment
- Read [Production](production.md) before going live
- Read [Workers](workers.md) if you need pull-based task delivery instead of webhooks
