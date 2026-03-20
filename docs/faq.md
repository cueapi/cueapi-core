# FAQ

## 1. What are the minimum system requirements?

- **CPU:** 1 vCPU
- **RAM:** 1 GB (2 GB recommended)
- **Disk:** 10 GB for the application, plus whatever your database needs
- **Software:** Docker and Docker Compose, or Python 3.11+, PostgreSQL 14+, and Redis 6+

CueAPI is lightweight. A small VPS (e.g., 2 vCPU / 2 GB RAM) can handle thousands of cues comfortably.

## 2. Can I run everything on one server?

Yes. The default `docker-compose.yml` runs the web server, poller, PostgreSQL, and Redis on a single machine. This is fine for small to medium workloads (up to ~10,000 cues). For higher scale or high availability, separate PostgreSQL and Redis onto dedicated instances.

## 3. How do I add more cues beyond the default limit?

Account-level cue limits are stored in the database. To increase the limit for a specific account, update the `accounts` table directly:

```sql
UPDATE accounts SET max_cues = 1000 WHERE id = 'usr_...';
```

There is no hard upper bound enforced by the system beyond what your database can handle.

## 4. What happens if the poller crashes?

The poller is responsible for checking which cues need to fire. If it crashes:

- No new executions will be created until the poller restarts.
- Already-queued webhook deliveries and worker executions continue processing normally.
- When the poller restarts, it catches up on any cues that should have fired during the downtime.

Run the poller under a process supervisor (Docker restart policy, systemd, etc.) so it restarts automatically.

## 5. Are webhook deliveries guaranteed?

CueAPI uses a transactional outbox pattern. When a cue fires, the execution is written to the database in the same transaction. The outbox processor then delivers the webhook. This means:

- **At-least-once delivery.** If a webhook delivery fails or the outbox processor crashes mid-delivery, the execution will be retried. Your endpoint may receive the same payload more than once.
- **Not exactly-once.** Design your webhook handlers to be idempotent. Use the execution ID in the payload to deduplicate.

Failed deliveries are retried with exponential backoff.

## 6. Can I use MySQL instead of PostgreSQL?

No. CueAPI relies on PostgreSQL-specific features (advisory locks, `FOR UPDATE SKIP LOCKED`, interval arithmetic). MySQL is not supported and there are no plans to add support.

## 7. How do I monitor CueAPI?

- **Health check:** `GET /health` returns `{"status": "ok"}` if the web process is alive.
- **Status:** `GET /status` returns database and Redis connectivity, poller heartbeat, and queue depth.
- **Logs:** CueAPI logs to stdout in structured format. Ship logs to your preferred aggregator (Datadog, Grafana Loki, ELK, etc.).
- **Key things to watch:** `pending_outbox` count, `stale_executions` count, poller heartbeat age, webhook failure rate.

See [Production](production.md) for details.

## 8. Can I run CueAPI without Redis?

No. Redis is required for the outbox queue, distributed locking, and poller coordination. Without Redis, the poller and webhook delivery system will not function. Redis does not need to be large -- a few hundred MB is sufficient for most workloads.

## 9. How do I upgrade to a new version?

1. Read the release notes for any breaking changes.
2. Run database migrations: `alembic upgrade head`
3. Deploy the new containers.

For zero-downtime upgrades, run migrations first, then do a rolling update of your containers. See [Production](production.md) for the full procedure.

## 10. Where do I get help?

- **GitHub Issues:** [github.com/govindkavaturi-art/cueapi-core/issues](https://github.com/govindkavaturi-art/cueapi-core/issues) -- for bug reports and feature requests.
- **Discussions:** Use GitHub Discussions for questions and community help.
- **Documentation:** Start with the [Quickstart](quickstart.md), then read [Configuration](configuration.md) and [Production](production.md).
