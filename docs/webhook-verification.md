# Webhook signature verification

When cueapi-core delivers a message via push (`Agent.webhook_url` is set), it POSTs the message envelope to the URL **and signs the request with HMAC-SHA256** using the agent's `webhook_secret`. Receivers must verify the signature before trusting the body — without verification, an attacker who learns the URL can impersonate cueapi-core and inject fake messages into your downstream pipeline.

This doc covers the signature shape, the verification algorithm, replay-attack protection, and reference implementations in Python and TypeScript.

> **Audience**: anyone implementing a webhook receiver for cueapi-core push delivery — including Dock's `cue-dock-svc.fly.dev`, OpenClaw Gateway in webhook mode, custom integrators wiring agents to their own message bus.

## TL;DR

For each incoming POST:

1. Read the `X-Cueapi-Signature` and `X-Cueapi-Signature-Timestamp` headers.
2. Reject if the timestamp is more than ±5 minutes from your server clock (replay window).
3. Compute `HMAC-SHA256(webhook_secret, "<timestamp>.<raw_body>")` and hex-encode it.
4. Constant-time compare against the `X-Cueapi-Signature` header value (without the `sha256=` prefix).
5. Reject (401) if anything mismatches; accept and process if all checks pass.

If the signature passes, the body is byte-for-byte what cueapi-core sent and was signed by someone holding the agent's `webhook_secret` — i.e. cueapi-core itself.

## Envelope shape

Every webhook delivery is a `POST` with these headers and a JSON body:

```http
POST /api/internal/cue/message-arrived HTTP/1.1
Host: cue-dock-svc.fly.dev
Content-Type: application/json
X-Cueapi-Signature: sha256=<64-hex-char-hmac>
X-Cueapi-Signature-Timestamp: 1714935600
X-Cueapi-Delivery-Id: dlv_<12-alphanumeric>
X-Cueapi-Event: message.delivered
X-Cueapi-Idempotency-Key: dlv_<delivery-id>
User-Agent: cueapi-core-worker/<commit-sha>

{
  "event": "message.delivered",
  "message": {
    "id": "msg_xxx",
    "from_agent_id": "agt_yyy",
    "to_agent_id": "agt_zzz",
    "thread_id": "msg_xxx",
    "subject": null,
    "body": "<message body>",
    "preview": "<truncated body>",
    "priority": 3,
    "metadata": {},
    "delivery_state": "delivering",
    "created_at": "2026-05-06T12:00:00Z"
  },
  "delivered_at": "2026-05-06T12:00:01Z"
}
```

**Header reference:**

| Header | Purpose |
|---|---|
| `X-Cueapi-Signature` | `sha256=<hex>` — HMAC-SHA256 of `<timestamp>.<raw_body>` keyed by `webhook_secret` |
| `X-Cueapi-Signature-Timestamp` | Unix epoch seconds at signing time. Use this for replay-window check, NOT your own clock-on-receive |
| `X-Cueapi-Delivery-Id` | Stable per-delivery identifier (`dlv_<12hex>`). Same on retries — use it for idempotency on your side |
| `X-Cueapi-Event` | Event name. Currently always `message.delivered`; future events (e.g. `message.acked`) keep the same envelope |
| `X-Cueapi-Idempotency-Key` | Mirrors `X-Cueapi-Delivery-Id`; convenience for receivers using a generic Idempotency-Key middleware |

## Why HMAC + timestamp (not bare HMAC)

Bare HMAC of just the body is replayable: an attacker who captures one signed request can replay it forever. Including the timestamp in the signed payload, plus rejecting old timestamps on the receive side, narrows the replay window to ±5 minutes (configurable per-receiver; cueapi-core itself doesn't enforce a window — that's the receiver's job).

The signed string is **`<timestamp>.<raw_body>`** with a literal `.` separator. The body is the **raw** request bytes — don't re-serialize JSON before signing or verifying, because key ordering differences between encoders will produce different bytes and fail comparison.

## Replay-attack protection

Always check `abs(now() - X-Cueapi-Signature-Timestamp) <= 300` (5 minutes) before computing the HMAC. Two reasons:

1. **Replay window**: an attacker who captured a signed request can replay it within the window but not after.
2. **Cheap rejection path**: timestamp compare is microseconds; HMAC compute is milliseconds. Reject obviously-stale requests before doing the expensive work.

For idempotency on your side (so retries from cueapi-core don't double-process), key your dedup table on `X-Cueapi-Delivery-Id`. cueapi-core's worker uses a stable delivery_id across retries.

## Reference implementations

### Python (FastAPI / Starlette)

```python
import hmac
import hashlib
import time
from fastapi import APIRouter, Header, HTTPException, Request
from typing import Annotated

router = APIRouter()

# Load this from your secrets store; rotate via cueapi-core's
# POST /v1/agents/{ref}/webhook-secret/regenerate.
WEBHOOK_SECRET = load_secret("cueapi_dock_agent_webhook_secret")

REPLAY_WINDOW_SECONDS = 300  # ±5 min


def _verify_signature(*, signature: str, timestamp: str, raw_body: bytes) -> bool:
    """Return True iff the HMAC matches AND the timestamp is fresh."""
    # 1. Replay window — reject obviously-stale before doing crypto work.
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(int(time.time()) - ts) > REPLAY_WINDOW_SECONDS:
        return False

    # 2. Compute HMAC over <timestamp>.<raw_body>.
    expected = hmac.new(
        WEBHOOK_SECRET.encode("utf-8"),
        f"{timestamp}.".encode("utf-8") + raw_body,
        hashlib.sha256,
    ).hexdigest()

    # 3. Constant-time compare. Strip the "sha256=" prefix.
    received = signature.removeprefix("sha256=")
    return hmac.compare_digest(expected, received)


@router.post("/api/internal/cue/message-arrived")
async def message_arrived(
    request: Request,
    x_cueapi_signature: Annotated[str, Header()],
    x_cueapi_signature_timestamp: Annotated[str, Header()],
    x_cueapi_delivery_id: Annotated[str, Header()],
):
    raw_body = await request.body()  # READ RAW BYTES, not parsed JSON
    if not _verify_signature(
        signature=x_cueapi_signature,
        timestamp=x_cueapi_signature_timestamp,
        raw_body=raw_body,
    ):
        raise HTTPException(status_code=401, detail="invalid signature")

    # Idempotency: skip if we've seen this delivery_id already.
    if await dedup_table.exists(x_cueapi_delivery_id):
        return {"status": "duplicate", "delivery_id": x_cueapi_delivery_id}
    await dedup_table.insert(x_cueapi_delivery_id, ttl_seconds=86_400)

    # Now safe to parse + process.
    payload = await request.json()
    await fan_out_to_drawers(payload["message"])
    return {"status": "accepted"}
```

### TypeScript / Node.js (Express)

```typescript
import express from "express";
import crypto from "crypto";

const WEBHOOK_SECRET = process.env.CUEAPI_DOCK_AGENT_WEBHOOK_SECRET!;
const REPLAY_WINDOW_SECONDS = 300;

function verifySignature(
  signature: string,
  timestamp: string,
  rawBody: Buffer,
): boolean {
  const ts = parseInt(timestamp, 10);
  if (Number.isNaN(ts)) return false;
  if (Math.abs(Math.floor(Date.now() / 1000) - ts) > REPLAY_WINDOW_SECONDS) {
    return false;
  }

  const expected = crypto
    .createHmac("sha256", WEBHOOK_SECRET)
    .update(`${timestamp}.`)
    .update(rawBody)
    .digest("hex");

  const received = signature.startsWith("sha256=")
    ? signature.slice("sha256=".length)
    : signature;

  // Constant-time compare; both buffers must be same length.
  if (expected.length !== received.length) return false;
  return crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(received));
}

const app = express();

// IMPORTANT: capture the raw body BEFORE express.json() parses it.
// crypto.createHmac is byte-sensitive; reserialized JSON won't match.
app.use(
  express.raw({ type: "application/json", limit: "1mb" }),
);

app.post("/api/internal/cue/message-arrived", async (req, res) => {
  const signature = req.header("X-Cueapi-Signature") ?? "";
  const timestamp = req.header("X-Cueapi-Signature-Timestamp") ?? "";
  const deliveryId = req.header("X-Cueapi-Delivery-Id") ?? "";
  const rawBody = req.body as Buffer;

  if (!verifySignature(signature, timestamp, rawBody)) {
    return res.status(401).json({ error: "invalid signature" });
  }

  if (await dedupTable.exists(deliveryId)) {
    return res.json({ status: "duplicate", delivery_id: deliveryId });
  }
  await dedupTable.insert(deliveryId, 86_400);

  const payload = JSON.parse(rawBody.toString("utf-8"));
  await fanOutToDrawers(payload.message);
  res.json({ status: "accepted" });
});
```

## Common verification mistakes

These are the patterns that cause "I can't get the signature to verify" support tickets — check each before opening one:

1. **Re-serializing JSON before HMAC.** Most web frameworks parse the request body into an object, then provide a `body` accessor that re-serializes on access. The re-serialized bytes have different key ordering, different whitespace, and won't match cueapi-core's signing input. **Always sign over the raw incoming bytes.** Both reference implementations above show how to capture them (Python: `await request.body()`; Node: `express.raw()` middleware).

2. **Including or stripping the `sha256=` prefix inconsistently.** cueapi-core sends `X-Cueapi-Signature: sha256=<hex>`. Strip the prefix once on receive; compare hex strings.

3. **Using `==` instead of constant-time compare.** Standard equality has timing-channel leakage. Use `hmac.compare_digest` (Python) or `crypto.timingSafeEqual` (Node).

4. **Forgetting the `.` separator in the signed string.** The signed payload is `"<timestamp>.<raw_body>"` — literal period between timestamp and body. Implementations that concatenate without the separator will fail verification.

5. **Validating against `Date.now()` instead of the request timestamp.** The replay-window check compares `now()` against the **request's** timestamp, not your server's previous request time.

6. **Trusting the body before signature verification.** Don't parse, log, or fan out the body until the signature is verified. A 401 path that logs the body to your standard pipeline can be exploited as an attacker-controlled-input injection vector.

## Rotating the secret

cueapi-core supports per-agent webhook-secret rotation:

```bash
curl -X POST $CUE_API/v1/agents/$AGENT_REF/webhook-secret/regenerate \
  -H "Authorization: Bearer $CUE_SK_KEY" \
  -H "X-Confirm-Destructive: true"
```

The response includes the new secret **inline once** (never returned again on read). Update your secrets store, redeploy, and the next webhook delivery will be signed with the new secret. There's no overlap window — old secret stops working immediately on rotation. Plan rotations during low-traffic windows or implement a brief dual-secret-acceptance period in your verifier.

## Versioning

The signature scheme described here is `v1` — implicit (no version header). If we change the scheme (different hash, different signed payload format) we'll add `X-Cueapi-Signature-Version: 2` and keep `v1` working for a deprecation window. Receivers should check the version header (treat absence as `v1`) and dispatch accordingly.

## Related

- [`docs/configuration.md`](configuration.md) — `webhook_url` + `webhook_secret` agent configuration
- [Cross-user message delivery semantics (messaging-v1.1.0+)](https://github.com/cueapi/cueapi-core/blob/main/CHANGELOG.md) — how the substrate decides which agents receive a message
- `POST /v1/agents/{ref}/webhook-secret/regenerate` — secret rotation endpoint
