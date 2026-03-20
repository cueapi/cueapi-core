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
  "api_key": "cue_live_..."
}
```

Save the `api_key` value. You need it for all subsequent requests.

## 4. Create a cue

Create a cue that fires every hour and sends a POST to your callback URL:

```bash
curl -X POST http://localhost:8000/v1/cues \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer cue_live_..." \
  -d '{
    "title": "Hourly ping",
    "schedule": "0 * * * *",
    "url": "https://example.com/webhook",
    "payload": {"message": "hello from cueapi"}
  }'
```

Expected response:

```json
{
  "id": "cue_...",
  "title": "Hourly ping",
  "schedule": "0 * * * *",
  "url": "https://example.com/webhook",
  "payload": {"message": "hello from cueapi"},
  "status": "active",
  "next_fire_at": "2025-01-01T01:00:00Z"
}
```

## 5. List your cues

```bash
curl http://localhost:8000/v1/cues \
  -H "Authorization: Bearer cue_live_..."
```

Expected response:

```json
{
  "items": [
    {
      "id": "cue_...",
      "title": "Hourly ping",
      "schedule": "0 * * * *",
      "status": "active",
      "next_fire_at": "2025-01-01T01:00:00Z"
    }
  ],
  "total": 1
}
```

## Next steps

- Browse the interactive API docs at [http://localhost:8000/docs](http://localhost:8000/docs)
- Read [Configuration](configuration.md) to customize your deployment
- Read [Production](production.md) before going live
- Read [Workers](workers.md) if you need pull-based task delivery instead of webhooks
