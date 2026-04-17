# Changelog

All notable changes to cueapi-core will be documented here.

## [Unreleased]

### Added
- **Verification modes** for cue outcomes. A new `verification: {mode: ...}` field on `CueCreate` / `CueUpdate` with five values: `none` (default), `require_external_id`, `require_result_url`, `require_artifacts`, `manual`. The outcome service computes `outcome_state` from (success, mode, evidence): missing required evidence lands in `verification_failed`, satisfied requirements land in `verified_success`, manual mode parks in `verification_pending`.
- **Inline evidence on `POST /v1/executions/{id}/outcome`.** `OutcomeRequest` now accepts `external_id`, `result_url`, `result_ref`, `result_type`, `summary`, `artifacts` alongside the existing `success` / `result` / `error` / `metadata`. Fully backward compatible — the legacy shape still works. The separate `PATCH /v1/executions/{id}/evidence` endpoint remains for two-step flows.
- **Migration 017** — `verification_mode` column on `cues` (String(50), nullable, CHECK-constrained enum). NULL and `none` are equivalent.
- **Alerts** — persisted alerts for `consecutive_failures`, `verification_failed`, and `outcome_timeout`. Three alert types are storage-ready; `consecutive_failures` and `verification_failed` fire automatically from `outcome_service.record_outcome`. `outcome_timeout` requires the deadline-checking poller (not yet in OSS) to activate; the CHECK constraint and router accept the type already.
- **Alert webhook delivery** — optional `alert_webhook_url` on the user. When set, each alert POSTs an HMAC-SHA256-signed payload to that URL. Fire-and-forget delivery; best-effort, never blocks outcome reporting. `X-CueAPI-Signature`, `X-CueAPI-Timestamp`, `X-CueAPI-Alert-Id`, `X-CueAPI-Alert-Type` headers. SSRF-protected at delivery time.
- **`GET /v1/alerts`** — list alerts for the authenticated user, with `alert_type` / `since` / `limit` / `offset` filters and per-user scoping.
- **`PATCH /v1/auth/me`** accepts `alert_webhook_url` (empty string clears; SSRF-validated at set time).
- **`GET /v1/auth/alert-webhook-secret`** — lazily generate + return the HMAC signing secret (64 hex chars).
- **`POST /v1/auth/alert-webhook-secret/regenerate`** — rotate the secret (requires `X-Confirm-Destructive: true`).
- **Dedup** — alerts collapse on `(user_id, alert_type, execution_id)` inside a 5-minute window.
- **Migrations 018 + 019** — alerts table with indexes and CHECK constraints; two columns on users.
- `examples/alert_webhook_receiver.py` — 30-line Flask receiver demonstrating signature verification.

### Changed
- **`POST /v1/executions/{id}/verify`** now accepts `{valid: bool, reason: str?}`. `valid=true` (default, preserving legacy behavior) transitions to `verified_success`; `valid=false` transitions to `verification_failed` and records the reason onto `evidence_summary` (truncated to 500 chars). Accepted starting states expanded to include `reported_failure`.
- `OutcomeResponse` now surfaces `outcome_state` in the response body.

### Restricted
- Worker-transport cues cannot currently combine with evidence-requiring verification modes (`require_external_id`, `require_result_url`, `require_artifacts`). Attempting to create or PATCH such a combination returns `400 unsupported_verification_for_transport`. `none` and `manual` are allowed for worker cues. This restriction will be lifted once cueapi-worker 0.3.0 (evidence reporting via `CUEAPI_OUTCOME_FILE`) is on PyPI.

## [0.1.2] - 2026-03-28

### Security
- Fixed IPv6-mapped IPv4 SSRF bypass (::ffff:127.0.0.1 bypassed SSRF validation)
- Updated aiohttp to 3.13.3 (16 CVEs fixed)
- Updated pyjwt to 2.12.0 (1 CVE fixed)
- Updated cryptography to 46.0.6 (3 CVEs fixed)
- Updated starlette to 0.47.2 (2 CVEs fixed)
- Disabled /docs, /redoc, /openapi.json in production (ENV=production)
- Added comprehensive SECURITY.md with architecture details and self-hosting hardening checklist
- Independent security audit completed: 41/42 tests passed, 1 critical finding fixed

## [0.1.1] - 2026-03-28

### Fixed
- Added missing migrations 011-014 that prevented fresh installs from starting
- Fixed migration chain gap between 010 and 015
- Corrected README: outcome reporting uses {"success": true} not {"outcome": "success"}
- Corrected README: one-time schedule field is `at` not `run_at`
- Corrected README: registration returns API key directly, no magic link required
- Documented pause/resume via PATCH {"status": "paused"} and {"status": "active"}
- Documented worker heartbeat endpoint and handlers array

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
