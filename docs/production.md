# Production Deployment

Guidelines for running CueAPI reliably in production.

## PostgreSQL

CueAPI requires PostgreSQL 14+.

**Connection pooling.** Place [PgBouncer](https://www.pgbouncer.org/) between CueAPI and PostgreSQL. Use transaction-level pooling. A pool size of 20-50 connections is sufficient for most workloads.

**Backups.** Enable continuous archiving with WAL-based backups (e.g., `pg_basebackup` + WAL archiving, or use a managed service with point-in-time recovery). Test restores regularly.

**WAL configuration.** If running your own PostgreSQL, set `wal_level = replica` and configure appropriate `max_wal_size` for your write volume. The poller and outbox processor generate steady write traffic.

## Redis

CueAPI uses Redis for the outbox queue, distributed locks, and poller coordination.

**Persistence.** Enable AOF persistence (`appendonly yes`) with `appendfsync everysec`. RDB snapshots alone are not sufficient -- you can lose up to the last snapshot interval of outbox entries on crash.

**Memory policy.** Set `maxmemory-policy noeviction`. CueAPI stores queue data in Redis that must not be evicted. Monitor memory usage and scale before hitting the limit.

**High availability.** For production, use Redis Sentinel or a managed Redis service with automatic failover. A single Redis instance is a single point of failure.

## Reverse proxy

Run CueAPI behind nginx, Traefik, Caddy, or your preferred reverse proxy.

**nginx example:**

```nginx
upstream cueapi {
    server 127.0.0.1:8000;
}

server {
    listen 443 ssl;
    server_name cueapi.example.com;

    ssl_certificate /etc/ssl/certs/cueapi.pem;
    ssl_certificate_key /etc/ssl/private/cueapi.key;

    location / {
        proxy_pass http://cueapi;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

**Traefik.** Add labels to your Docker Compose service:

```yaml
labels:
  - "traefik.enable=true"
  - "traefik.http.routers.cueapi.rule=Host(`cueapi.example.com`)"
  - "traefik.http.routers.cueapi.tls.certresolver=letsencrypt"
  - "traefik.http.services.cueapi.loadbalancer.server.port=8000"
```

## Sizing & cold-start considerations

CueAPI is fine on small hardware for low-volume deployments — the substrate is async + the hot path is single-digit-millisecond. But if you deploy on a tier with cold-start (Fly.io shared-cpu-1x, idle Railway instances, AWS Lambda-style scale-to-zero), the **first request after idle** can stall for several seconds. Plan caller timeouts accordingly.

### Observed cold-start (reference: Dock production)

Dock runs cueapi-core vendored on Fly.io `shared-cpu-1x:512MB`, single machine. Observations:

- **Idle-to-first-request latency: 3-5 seconds** when the machine has been idle long enough to spin down. This is Fly's machine restart, not cueapi-core's startup — once awake, hot-path latency is normal.
- **Caller timeout originally 1500ms** (their default). After observing first-poll timeouts on drawer-open under cold-start, **raised to 6000ms minimum**. This now consistently absorbs the cold-start window.

### Recommendations for low-tier deployments

1. **Budget caller timeouts at 6000ms minimum** if your tier has cold-start. SDK consumers (`cueapi-sdk`, `cueapi-cli`, `cueapi-mcp`, third-party HTTP clients) should configure their HTTP timeout accordingly. The default in `httpx` (5 seconds) is borderline — bump to 10s if you can.
2. **Keep the deployment warm** if cold-start hurts UX. Two patterns work:
   - **Min-machines: 2.** Run at least 2 instances. Most platforms (Fly, Railway, Render) won't spin both down at once. Latency is consistent at the cost of base running cost.
   - **External warm-pinger.** A cron-style job (GitHub Actions on a schedule, an upstream `cueapi monitor attach`-style daemon, or a separate uptime monitor) hits `GET /health` every 60-90 seconds. Costs less than min-machines: 2 but adds an external dependency.
3. **Document the cold-start budget in your client SDK / CLI / app** so end-users don't see surprising 5-second pauses. Surface "first request" UX states (loading skeletons, "warming up..." labels) when applicable.

### Sizing for higher tiers

For dedicated CPU + always-on deployments (Fly `dedicated-cpu-1x` or larger, Railway scaled-up plans, EKS/GKE):

- **2 vCPU + 1GB RAM** comfortably handles ~1000 cues/min + ~500 webhook deliveries/min on a single instance. The poller is the bottleneck; scale the poller process before the API process.
- **PostgreSQL**: 2GB RAM minimum, 4GB+ recommended. Indexes are partial + the hot tables (`cues`, `executions`, `messages`) are partition-friendly if you grow into millions of rows.
- **Redis**: 256MB sufficient for low-volume; 1GB+ for high-volume to avoid eviction pressure on the outbox. AOF persistence MUST stay on.

Scale horizontally (add API/worker/poller replicas) before scaling vertically — the substrate is designed for shared-nothing horizontal scaling.

### Health-check configuration on low-tier

If you're using `GET /health` (covered below) on a low-tier deployment, configure your orchestrator to use **5-second initial-delay + 10-second interval** so the cold-start doesn't trigger spurious failure markers on first deploy.

## Health checks

CueAPI exposes two health endpoints:

| Endpoint | Purpose |
|---|---|
| `GET /health` | Basic liveness check. Returns `{"status": "ok"}` if the web process is running. Use this for container orchestrator liveness probes. |
| `GET /status` | Detailed readiness check. Returns database connectivity, Redis connectivity, and poller status. Use this for readiness probes and monitoring dashboards. |

Configure your load balancer or orchestrator to check `/health` every 10 seconds with a 5-second timeout.

## Monitoring

Key metrics to watch:

| Metric | What it means | Action if abnormal |
|---|---|---|
| `pending_outbox` | Number of outbox entries waiting to be delivered. | If growing steadily, the outbox processor is falling behind. Check webhook endpoint availability and increase concurrency. |
| `stale_executions` | Executions that have been claimed but not completed within the timeout. | Indicates worker failures or timeouts. Increase `WORKER_CLAIM_TIMEOUT_SECONDS` or investigate worker health. |
| Poller heartbeat | Timestamp of the poller's last successful cycle. | If stale (> 2x `POLLER_INTERVAL_SECONDS`), the poller process has stopped. Restart it. |
| Webhook failure rate | Percentage of webhook deliveries returning non-2xx responses. | Investigate target endpoint health. Consider switching to worker transport for unreliable endpoints. |
| Database connection count | Active connections to PostgreSQL. | If approaching pool limits, increase pool size or optimize query patterns. |

Query these from the `/status` endpoint or directly from the database and Redis.

## Upgrading

### Database migrations

CueAPI uses Alembic for database migrations. Before starting the new version:

```bash
alembic upgrade head
```

Run this as a separate step before deploying new containers. Do not run migrations concurrently from multiple instances.

### Zero-downtime deploys

1. Run `alembic upgrade head` against the database.
2. Deploy new containers alongside old ones (rolling update).
3. The old and new versions share the same database and Redis. Migrations are backward-compatible within a minor version.
4. Once all old containers are drained, the deploy is complete.

If using Docker Compose, a simple approach:

```bash
alembic upgrade head
docker compose pull
docker compose up -d --no-deps --build web poller
```

## Security

**Disable open registration.** Set `ALLOW_REGISTER=false` after creating your accounts. This prevents unauthorized account creation on your instance.

**SESSION_SECRET.** Generate a unique, high-entropy secret (`openssl rand -hex 32`). Never commit it to source control. Rotate it periodically -- rotation will invalidate all active sessions.

**HTTPS.** Always terminate TLS at your reverse proxy. Never expose CueAPI over plain HTTP in production.

**Network isolation.** Place PostgreSQL and Redis on a private network. They should not be reachable from the public internet.

**API keys.** API keys are bearer tokens. Treat them like passwords. Store them in environment variables or a secrets manager, not in code or config files.

**Rate limiting.** CueAPI includes built-in rate limiting middleware (`app/middleware/rate_limit.py`) using a Redis-backed sliding window. Default behavior:

- **Authenticated requests:** 60 requests per minute per API key. Tier-specific limits are read from the user's auth cache if available.
- **Unauthenticated requests:** 60 requests per minute per IP address.
- **Exempt paths:** `/health`, `/status`, `/docs`, `/openapi.json`, `/v1/billing/webhook`, `/v1/blog/*`, `/v1/internal/*`
- **Response headers:** `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`, and `Retry-After` (on 429).
- **Graceful degradation:** If Redis is unavailable, rate limiting is skipped and all requests are allowed.

For additional protection (e.g., DDoS mitigation, geo-blocking), configure rate limits at your reverse proxy layer as well.
