# Security Policy

## Reporting Vulnerabilities

Email: security@vector.build
Expected response: 48 hours
We do not pursue legal action against good-faith security researchers.

## Security Architecture

### API Key Management
- Keys generated with cryptographically secure random (secrets.token_hex)
- Stored as SHA-256 hashes - plaintext never hits the database
- Fernet encryption (AES) for recoverable key reveal feature
- Instant revocation on rotation with Redis cache invalidation

### SSRF Protection
- Creation-time validation: blocks private IPs, loopback, link-local, cloud metadata, CGN ranges, IPv6-mapped IPv4
- Delivery-time re-validation: catches DNS rebinding attacks
- Blocked hostnames: localhost, metadata.google.internal, metadata.internal
- Credentials in callback URLs rejected
- Redirect following disabled on webhook delivery
- Production mode enforces HTTPS-only callbacks

### Webhook Signing
- HMAC-SHA256 with timestamp binding
- 5-minute replay window
- Timing-safe comparison (hmac.compare_digest)
- Per-user signing secrets with rotation support

### Rate Limiting
- Sliding window via Redis sorted sets
- Per-API-key (tier-based) and per-IP (unauthenticated)
- Standard headers: X-RateLimit-Limit, X-RateLimit-Remaining, Retry-After
- Rejected requests do not inflate the rate window

### Input Validation
- 1MB request body limit (raw ASGI middleware, catches chunked transfers)
- 1MB payload limit per cue
- 10KB metadata limit on outcomes
- Null byte rejection (PostgreSQL JSONB compatibility)
- Cron expression and timezone validation

### Data Isolation
- All resources scoped to user_id - no cross-tenant access
- 404 (not 403) on unauthorized resource access - prevents enumeration
- FOR UPDATE locks on outcome recording - prevents race conditions
- Conditional UPDATE WHERE for execution claims - prevents double-claim

## Self-Hosting Hardening Checklist

- [ ] Change SESSION_SECRET from default value
- [ ] Set ENV=production (enforces HTTPS-only callbacks)
- [ ] Set strong PostgreSQL password
- [ ] Configure CORS_ORIGINS for your dashboard domain
- [ ] Disable /docs and /openapi.json in production
- [ ] Add security headers (CSP, X-Frame-Options, HSTS)
- [ ] Keep dependencies updated (pip-audit -r requirements.txt)
- [ ] Run behind a reverse proxy with TLS termination

## Audit History

| Date | Auditor | Scope | Result |
|---|---|---|---|
| March 28, 2026 | Independent AI Security Agent | Full pen test - 50+ tests, OWASP ZAP, Nuclei, SSRF, auth, input validation | 41/42 passed, 1 critical fixed |
