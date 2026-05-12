# Body integrity verification

cueapi-core ships an opt-in body-integrity primitive that protects against silent body-content corruption — most commonly caller-side shell expansion of metacharacters (`$(...)`, backticks, `${VAR}`) in body arguments BEFORE the request leaves the caller's environment.

This is the open-core substrate behind the SDK-level auto-verify in [cueapi-python](https://github.com/cueapi/cueapi-python) and [cueapi-cli](https://github.com/cueapi/cueapi-cli). Self-hosters running cueapi-core get the substrate for free; their SDKs talk to it the same way the hosted SDKs do.

## The bug class this prevents

```bash
BODY="message with $(date) timestamp"
curl -X POST https://your-cueapi-instance/v1/messages \
  -H "Authorization: Bearer $CUEAPI_API_KEY" \
  -H "X-Cueapi-From-Agent: $SENDER" \
  -H "Content-Type: application/json" \
  -d "{\"to\":\"$RECIPIENT\",\"body\":\"$BODY\"}"
```

Bash evaluates `$(date)` at variable-assignment time. The body you THOUGHT you were sending is gone before curl runs. The substrate accepts the mutated JSON with HTTP 200. The recipient sees corrupted content. No fail-loud signal anywhere in the pipeline.

LLM-generated curl examples, CI scripts, and bash automation reproduce the pattern at scale. The verify-echo primitive surfaces it loudly.

## How it works

Add `X-CueAPI-Verify-Echo: true` to any POST/PATCH/PUT request with a body. The substrate echoes the body it received back in the response under `body_received` (plus a SHA-256 hex digest under `body_received_sha256` for constant-cost compare):

```bash
curl -X POST https://your-cueapi-instance/v1/messages \
  -H "Authorization: Bearer $CUEAPI_API_KEY" \
  -H "X-Cueapi-From-Agent: $SENDER" \
  -H "X-CueAPI-Verify-Echo: true" \
  -H "Content-Type: application/json" \
  -d @/tmp/body.json
```

Response (truncated):

```json
{
  "id": "msg_xxx",
  "delivered": true,
  "body_received": "the verbatim body the server received",
  "body_received_sha256": "<64-hex sha256 of body field bytes>"
}
```

Caller computes `sha256(body_bytes)` locally; compares to `body_received_sha256`; throws on drift.

The header is opt-in. Clients without it see no behavior change — `body_received` and `body_received_sha256` are absent from the response.

## Substrate architecture

Two layers ship in cueapi-core:

**Layer 1** — Endpoint-specific echo (Phase 1): `POST /v1/messages` and `POST /v1/cues/{cue_id}/fire` extract their canonical string body field (`MessageCreate.body`, `payload_override.message` if present on fire) and echo it as a STRING. SDKs verify against the string directly.

**Layer 1.5** — Universal middleware (Phase 2): all other POST/PATCH/PUT endpoints get echo coverage via `app/middleware/verify_echo.py`. The middleware echoes the parsed request body as a JSON object. 52 endpoints covered; method-gated (GET ignored); status-gated (4xx/5xx not echoed; validation errors stay clean).

| Endpoint shape | `body_received` shape |
|---|---|
| `POST /v1/messages` | STRING — `MessageCreate.body` verbatim |
| `POST /v1/cues/{id}/fire` | STRING — `payload_override.message` (or null when no body) |
| All other POST/PATCH/PUT | OBJECT — parsed request body as JSON |

`body_received_sha256` is always a 64-character hex string of those exact UTF-8 bytes.

## SDK auto-verify

The official SDKs use `X-CueAPI-Verify-Echo: true` automatically and raise on mismatch:

| SDK | Messages | Cues fire | Opt-out |
|---|---|---|---|
| **cueapi-python** | default-on | opt-in (`auto_verify=True`) | `auto_verify=False` |
| **cueapi-cli** | default-on | opt-in (`--verify`) | `--no-verify` |
| **cueapi-mcp** | default-on | opt-in (`auto_verify: true`) | omit flag |
| **cueapi-action** | default-on (`no-verify: "true"` opts out) | opt-in (`verify: "true"`) | — |

Why fire is opt-in everywhere: the substrate's `/v1/cues/{cue_id}/fire` echoes a pydantic-after-parse body that may include server-side default-population, causing spurious diff vs the SDK's canonical-JSON serialization. The default-off design avoids false-positive mismatches; callers opt in when they know substrate echo matches their serialization (typical for the sha256 constant-cost path).

## Defense-in-depth layers (recap)

For full coverage, layer this substrate with the SDK + caller patterns:

1. **Substrate** (this guide) — `X-CueAPI-Verify-Echo` echo-back. Open-core; ships in cueapi-core.
2. **SDK auto-verify** — clients use the header automatically and raise on mismatch.
3. **Force-file mode** — `cueapi-cli messages send --message-file <path>` reads bytes verbatim; rejects inline `--body` with shell metacharacters unless `--allow-inline-metachars` is set.
4. **Documentation** — guides lead with file-payload pattern, not inline strings.

Self-hosters running cueapi-core get layers 1 + 2 automatically by using the published SDKs. Layer 3 ships in cueapi-cli; layer 4 is this guide.

## When to disable

Disable verify-echo only when:

- **Perf-sensitive flows** at the very high QPS limit. The echo doubles response-payload bytes; a sustained outbound stream may want to opt out for the 5-10% saving.
- **Streaming use cases** where the response body shape is locked downstream and adding fields breaks compatibility.

Opt-out is per-request (SDK kwarg or CLI flag); there's no global server-side disable.

## Implementation references

- `app/utils/verify_echo.py` — STRING-shape helper (Layer 1)
- `app/middleware/verify_echo.py` — universal middleware (Layer 1.5)
- `app/routers/messages.py`, `app/routers/cues.py` — Phase 1 endpoint integration

Tests pin the shape:

- `tests/test_verify_echo.py` — Layer 1 endpoint coverage
- `tests/test_verify_echo_middleware.py` — Layer 1.5 method/status/content-type gating + idempotency-when-handler-already-injected
- `tests/test_verify_endpoints.py` — verify-result endpoints

## Background

Added 2026-05-11 (Mike body-verify directive). Substrate primitive Layer 1 from cueapi-core #86; Layer 1.5 universal middleware from #87; STRING-shape spec-lock from #88 (echo-shape hotfix for `body_received` field on messages endpoint). Cross-stack SDK Phase 2 + caller force-file + this docs guide constitute the four-layer defense.
