# Workers

Workers are a pull-based alternative to webhooks. Instead of CueAPI pushing an HTTP request to your endpoint, your worker process polls CueAPI for pending executions, processes them, and reports the outcome.

## When to use workers vs webhooks

| | Webhooks | Workers |
|---|---|---|
| Setup | Provide a public URL | Run a worker process |
| Network | CueAPI must reach your endpoint | Worker must reach CueAPI |
| Best for | Simple integrations, serverless | Long-running tasks, firewalled environments, AI agents |
| Timeout | Limited by `WEBHOOK_TIMEOUT_SECONDS` | Limited by `WORKER_CLAIM_TIMEOUT_SECONDS` (default 15 min) |

Use workers when:
- Your tasks run longer than 30 seconds
- Your processing environment is behind a firewall or NAT
- You want fine-grained control over concurrency and retries
- You are orchestrating AI agent workflows

## Creating a worker-transport cue

Set `transport` to `"worker"`. No `url` is needed.

```bash
curl -X POST http://localhost:8000/v1/cues \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer cue_sk_..." \
  -d '{
    "name": "process-reports",
    "schedule": {"type": "recurring", "cron": "0 9 * * *", "timezone": "UTC"},
    "transport": "worker",
    "payload": {"report_type": "daily"}
  }'
```

## Worker lifecycle

### 1. Poll for claimable executions

```bash
curl http://localhost:8000/v1/executions/claimable \
  -H "Authorization: Bearer cue_sk_..."
```

Response:

```json
{
  "executions": [
    {
      "execution_id": "exec_abc123",
      "cue_id": "cue_...",
      "cue_name": "process-reports",
      "payload": {"report_type": "daily"},
      "scheduled_for": "2025-01-01T09:00:00Z",
      "attempt": 1
    }
  ]
}
```

If no executions are available, `executions` will be an empty array. Poll again after a short delay.

### 2. Claim an execution

```bash
curl -X POST http://localhost:8000/v1/executions/exec_abc123/claim \
  -H "Authorization: Bearer cue_sk_..." \
  -H "Content-Type: application/json" \
  -d '{"worker_id": "my-worker-001"}'
```

Response:

```json
{
  "id": "exec_abc123",
  "status": "claimed",
  "claimed_at": "2025-01-01T09:00:01Z"
}
```

Once claimed, no other worker can claim the same execution. The claim is held for `WORKER_CLAIM_TIMEOUT_SECONDS` (default 900 seconds / 15 minutes). If you do not report an outcome before the timeout, the execution is released back to the queue.

### 3. Do your work

Process the payload however you need. This is your application logic.

### 4. Report the outcome

**Success:**

```bash
curl -X POST http://localhost:8000/v1/executions/exec_abc123/outcome \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer cue_sk_..." \
  -d '{
    "success": true,
    "result": "rows_processed: 142"
  }'
```

**Failure:**

```bash
curl -X POST http://localhost:8000/v1/executions/exec_abc123/outcome \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer cue_sk_..." \
  -d '{
    "success": false,
    "error": "Database connection refused"
  }'
```

## Running multiple workers

You can run as many worker processes as you need. The claim mechanism ensures each execution is processed by exactly one worker. Workers can run on different machines, in different regions, or in different containers.

Tips for multi-worker setups:
- Each worker polls independently. Stagger poll intervals slightly to reduce contention.
- A typical poll interval is 1-5 seconds. Adjust based on your latency requirements.
- Workers do not need to coordinate with each other. CueAPI handles distribution.

## Timeouts and heartbeats

Two timeouts govern worker behavior:

- **`WORKER_HEARTBEAT_TIMEOUT_SECONDS`** (default 180): If a worker has not polled `/v1/executions/claimable` within this window, CueAPI considers it stale. Executions claimed by stale workers are released.

- **`WORKER_CLAIM_TIMEOUT_SECONDS`** (default 900): Maximum time a worker can hold a claimed execution. If no outcome is reported within this window, the execution is released back to the queue for another worker to claim.

Set `WORKER_CLAIM_TIMEOUT_SECONDS` to be longer than your longest expected task duration. If your tasks routinely take 30 minutes, set it to at least 2100 (35 minutes).

## Minimal worker example (Python)

```python
import time
import requests

API = "http://localhost:8000"
TOKEN = "cue_sk_..."
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

while True:
    # Poll
    resp = requests.get(f"{API}/v1/executions/claimable", headers=HEADERS)
    executions = resp.json().get("executions", [])

    for execution in executions:
        exec_id = execution["execution_id"]

        # Claim
        claim = requests.post(
            f"{API}/v1/executions/{exec_id}/claim",
            headers={**HEADERS, "Content-Type": "application/json"},
            json={"worker_id": "my-worker-001"},
        )
        if claim.status_code != 200:
            continue

        # Process
        try:
            result = do_work(execution["payload"])
            requests.post(
                f"{API}/v1/executions/{exec_id}/outcome",
                headers={**HEADERS, "Content-Type": "application/json"},
                json={"success": True, "result": str(result)},
            )
        except Exception as e:
            requests.post(
                f"{API}/v1/executions/{exec_id}/outcome",
                headers={**HEADERS, "Content-Type": "application/json"},
                json={"success": False, "error": str(e)},
            )

    time.sleep(2)
```
