# Hosted-only features

cueapi-core is the open-source primitive. [cueapi.ai](https://cueapi.ai) is the hosted product built on top. Some capabilities are intentionally hosted-only; this document explains which and why.

The line is drawn at: **scheduling + delivery + outcome reporting is OSS. Everything that's a SaaS business layer, a paid-API integration, or experimental product surface is hosted-only.** If you're self-hosting, you're running the same scheduling/delivery/outcome engine that powers cueapi.ai — no crippled OSS tier.

## Hosted-only capabilities

| Feature | Why hosted-only |
|---|---|
| Stripe billing | SaaS business layer. Self-hosters manage their own limits and don't need a payment processor. |
| GDPR endpoints (deletion, export, processing records) | Hosted-service compliance obligation. Self-hosters own their own legal surface and know which jurisdictions apply to them; a one-size policy baked into OSS would be wrong for most. |
| Blog content pipeline | Marketing infrastructure for cueapi.ai. Not a general-purpose feature. |
| Memory blocks | Product experiment not yet public. May graduate to OSS once the shape stabilizes. |
| Support tickets → GitHub issues routing | cueapi.ai customer-support automation. Self-hosters file issues directly on this repo. |
| Jenny docs chatbot | cueapi.ai-specific documentation UI. |
| Deploy hook (Railway staging) | cueapi.ai CI/CD infrastructure. |
| Dashboard (React UI) | Hosted-only. cueapi-core is API-first — build your own UI, or use the hosted dashboard at cueapi.ai. |
| Email alert delivery (SendGrid) | Paid-API integration. OSS ships **webhook-based** alert delivery instead: configure an `alert_webhook_url` on your user and forward alerts to your own Slack/Discord/ntfy/SMTP-relay pipeline. See README's "Alerts" section. |

## What's in cueapi-core

Everything needed to run a production scheduler with outcome tracking:

- Cue CRUD, scheduling, cron parsing, timezone handling
- Execution lifecycle, worker transport, webhook transport, heartbeats, replays
- Outcome reporting with verification modes and evidence
- Webhook HMAC signing, SSRF protection, retry-with-backoff
- Alert firing (via webhook delivery; add your own `alert_webhook_url`)
- API keys, device-code auth, session refresh, rate limiting
- At-least-once delivery via transactional outbox

Full feature list: see [README.md](README.md).

## Contributing a port

If you need a hosted-only feature in cueapi-core, open a GitHub issue with:

1. Your use case (what you're building, what breaks without it)
2. A rough idea of the OSS-compatible design (e.g. "swap SendGrid for a pluggable `AlertDeliveryBackend` interface")
3. Whether you'd be willing to submit the PR yourself

Community-driven ports are welcome. The hosted-only list is not permanent — features may move to OSS over time based on demand and on whether a self-hostable design exists.

## Maintainer note

If you're a cueapi maintainer porting a private-monorepo change:

- Check [`parity-manifest.json`](parity-manifest.json) to see whether the file you're touching has an OSS counterpart.
- The `parity-check` GitHub Action posts a soft warning on PRs that modify tracked files, prompting you to link the OSS PR (or file a follow-up issue).
- See the private monorepo's internal docs for the reverse direction — what to sync when OSS changes first.
