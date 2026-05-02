# Changelog

All notable changes to cueapi-core will be documented here.

## [Unreleased]

### Added (messaging primitive port — 2026-05-01)

- **Messaging primitive v1** (Phase 2.11 / 12.1.5 in the private monorepo, ported to OSS as a single coherent feature). Persistent identity-addressed messages between agents, on top of the existing scheduling/delivery infrastructure.
- **Identity** — new `agents` table. Each row is one addressable Identity: `agt_<12 alphanum>` PK, per-user-unique `slug`, optional `webhook_url` + `webhook_secret` (paired). `users.slug` column added so agents can be addressed in slug-form (`agent@user_slug`) as well as by opaque ID. POST/GET/PATCH/DELETE `/v1/agents` + `/v1/agents/{id}/webhook-secret` for rotation.
- **Messages** — new `messages` table with delivery state machine: `queued → delivering → delivered → read → acked`, plus `retry_ready` / `failed` / `expired` branches. POST `/v1/messages` (with `Idempotency-Key` header dedup, server-side body fingerprinting, 24h dedup window) + GET `/v1/messages/{id}` + state transitions (`/read`, `/ack`).
- **Inbox** — GET `/v1/agents/{id}/inbox` is THE delivery surface for poll-based agents. Atomic `queued → delivered` transition on poll-fetch via single `UPDATE ... RETURNING ...`. State filtering, thread filtering, pagination, count-only mode.
- **Sent view** — GET `/v1/agents/{id}/sent` for sender-side audit. Default state filter includes terminal states (acked/expired) so sent history doesn't disappear when recipient acks.
- **Reply threading** — `Message.thread_id` + `Message.reply_to`. Root messages have `thread_id == id`; replies inherit `thread_id` from parent via `reply_to` lookup. Server-side; consumers don't manage thread state.
- **Push delivery** — when a recipient agent has `webhook_url` set, sends are also enqueued to the existing transactional outbox for HMAC-SHA256-signed POST to the recipient's URL. Headers: `X-CueAPI-Signature`, `X-CueAPI-Timestamp`, `X-CueAPI-Event-Type: message.created`, `X-CueAPI-Message-Id`, `X-CueAPI-Agent-Id`, `X-CueAPI-Thread-Id`, `X-CueAPI-Attempt`. SSRF-protected at delivery time (DNS rebind defense).
- **Retry + stale-recovery** — push deliveries that fail with retryable errors (5xx / 502 / 503 / 408 / 429 / TLS / DNS / timeout / connection-refused) reschedule via `dispatch_outbox.scheduled_at` with backoff `[1, 5, 15]` minutes. 4xx-terminal (401 / 404 / 405) goes straight to `failed`. `Retry-After` header honored on 429 / 503 with `max(own_min, retry_after)` formula. Worker-crash-mid-delivery handled by a stale-recovery poll loop that scans messages stuck in `delivering` past the 5-minute threshold.
- **Per-user concurrent delivery cap** — shared with cue webhook deliveries via the `concurrent:{user_id}` Redis counter (`MAX_CONCURRENT_DELIVERIES_PER_USER`, default 50). Over-cap message dispatches recycle via a fresh outbox row at `scheduled_at = now+30s`.
- **Quotas** — new `usage_messages_monthly` table (mirrors `usage_monthly` shape) + `users.monthly_message_limit` column (default 300). Separate from execution quotas.
- **Cleanup tasks** — `worker/message_cleanup.py` provides `expire_old_messages` (TTL → expired), `cleanup_expired_messages` (hard-delete 7d after terminal), `free_old_idempotency_keys` (NULL keys after 24h to free the unique partial index for reuse). All dry-run by default; pass `dry_run=False` to act.
- **Migrations 020 + 021 + 022** — Identity tables and columns, Messages table + dispatch_outbox extension + quotas, push-retry columns. Renumbered from the private monorepo's 043 / 044 / 045 because the OSS repo has its own migration history.
- 152 new tests (test_agents, test_messages, test_inbox, test_message_quotas, test_message_cleanup, test_messaging_classification, test_messaging_push_delivery, test_messaging_push_retry, test_messaging_push_concurrent_cap, test_messaging_push_enqueue, test_messaging_schema). Total test suite: 618 passed, 0 failed.

### Deviations from the private monorepo

- **`api_key_id` columns omitted from `agents` and `messages` tables.** The private monorepo has multi-key scoping (its own `api_keys` table with FK from agents and messages). cueapi-core uses single-key auth (`users.api_key_hash`); messaging never used `api_key_id` for business logic — it was an audit-only field. Documented in `parity-manifest.json` under `multi_key_scoping_omission` and `HOSTED_ONLY.md`. If multi-key scoping is ever ported, follow-up migrations can ADD COLUMN those FKs.
- **GDPR-cascade safety harness omitted from cleanup tasks.** Private's `worker/gdpr_cleanup.py` requires `GDPR_LAST_BACKUP_AT` env var to run cleanup in real (non-dry-run) mode. cueapi-core's `worker/message_cleanup.py` strips this — self-hosters opt in to real action by passing `dry_run=False` directly.
- **`test_message_gdpr.py` not ported** — tested the GDPR cascade-deletion path that's hosted-only.

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
- `HOSTED_ONLY.md` documenting the open-core policy — which features are OSS, which are intentionally hosted-only on cueapi.ai, and why.
- `parity-manifest.json` enumerating files that have a same-path counterpart in the private cueapi monorepo. Used by the new parity-check workflow.
- `.github/workflows/parity-check.yml` — soft-enforcement CI that posts a comment on PRs which touch tracked files, asking the author to cross-reference the private repo. Never blocks merge; exits 0 regardless.
- README "Open core model" section near the top, linking to `HOSTED_ONLY.md`.

### Changed
- **`POST /v1/executions/{id}/verify`** now accepts `{valid: bool, reason: str?}`. `valid=true` (default, preserving legacy behavior) transitions to `verified_success`; `valid=false` transitions to `verification_failed` and records the reason onto `evidence_summary` (truncated to 500 chars). Accepted starting states expanded to include `reported_failure`.
- `OutcomeResponse` now surfaces `outcome_state` in the response body.

### Removed
- **Rejection of `(worker transport, require_*)` verification combos** has been lifted. cueapi-worker 0.3.0 (released 2026-04-17 to PyPI) closes the evidence gap via `$CUEAPI_OUTCOME_FILE`: handlers write evidence JSON to a per-run temp file; the daemon reads the file after exit and merges into the outcome POST. All five verification modes now work on both transports. Operators running older cueapi-worker versions should upgrade via `pip install --upgrade cueapi-worker` — until they do, evidence-requiring modes on worker cues will land every execution in `verification_failed`.

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
