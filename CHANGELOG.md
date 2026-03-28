# Changelog

All notable changes to cueapi-core will be documented here.

## [0.1.0] - 2025-03-28

### Added
- Initial open source release
- 26 REST API endpoints across 8 resource groups (auth, cues, executions, workers, webhook secrets, usage, health)
- Transactional outbox pattern for at-least-once delivery
- Webhook transport with signed payloads (HMAC-SHA256)
- Worker transport for agents without a public URL
- Exponential backoff retries (1, 5, 15 min)
- Email and webhook failure alerts
- Execution outcome tracking separate from delivery status
- PostgreSQL 16 + Redis 7 stack
- Docker Compose for local development
- Magic link authentication
- Device code flow for CLI/agent auth
- Memory block endpoint (GET /v1/memory-block)
- Usage endpoint with projected limits (GET /v1/usage)
- 367 automated tests via Argus QA pipeline
