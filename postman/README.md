# CueAPI Postman Collection

Official Postman collection for the CueAPI REST API, auto-generated from the live OpenAPI spec at [`https://api.cueapi.ai/openapi.json`](https://api.cueapi.ai/openapi.json).

## Contents

- `cueapi.postman_collection.json` — importable Postman collection (v2.1), 16 folders, 67 requests, organized by API tag.

## Import

**Postman desktop:**

1. Open Postman → **Import** → drop in `cueapi.postman_collection.json`.
2. Set the `apiKey` collection variable to your CueAPI API key (generate one at [cueapi.ai](https://cueapi.ai)).
3. The `baseUrl` variable defaults to `https://api.cueapi.ai`; override for self-hosted CueAPI.

**Postman CLI / Newman:**

```bash
newman run postman/cueapi.postman_collection.json \
  --env-var apiKey=$CUEAPI_API_KEY \
  --env-var baseUrl=https://api.cueapi.ai
```

## Auth

The collection is configured with collection-level Bearer auth using the `{{apiKey}}` variable. Every request inherits it automatically — no per-request header setup needed.

## Regeneration

The collection regenerates cleanly from the live OpenAPI spec. From the repo root:

```bash
curl -sS https://api.cueapi.ai/openapi.json -o /tmp/cueapi-openapi.json
npm install -g openapi-to-postmanv2
openapi2postmanv2 -s /tmp/cueapi-openapi.json \
  -o postman/cueapi.postman_collection.json \
  -O folderStrategy=Tags,requestParametersResolution=Example
```

## Links

- **Homepage:** https://cueapi.ai
- **Docs:** https://docs.cueapi.ai
- **Public Workspace (Postman API Network):** *pending publish*
