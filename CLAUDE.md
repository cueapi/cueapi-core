# Claude Code Rules

## Who Does What

**Claude Code** - writes code, opens PRs, merges when CI passes, deploys to staging and frontend sites
**Argus** - tests staging, promotes to production, cuts PyPI release tags, tests docs and blog accuracy

## PR Process - Non-Negotiable

Every change must go through a PR. No direct pushes to main under any circumstances.
PRs are the permanent audit trail of every change made to this codebase.
Claude Code opens PRs and merges them once CI passes. No human reviewer required.
CI must pass before merging - no exceptions.
Never bypass branch protection or force merge.
Never merge a failing CI check - fix the root cause first.

## Complete Pipeline

### cueapi (hosted API - private repo)
Claude Code: code change -> open PR -> sdk-integration + deploy-staging CI must pass -> merge -> Railway staging deploy
Argus: runs 246 staging tests -> all pass -> promotes to production

### cueapi-core (open source)
Claude Code: code change -> open PR -> sdk-integration CI must pass -> merge
Argus: runs full pytest suite -> confirms green

### cueapi-python
Claude Code: code change -> open PR -> sdk-integration CI must pass -> merge
Argus: runs SDK tests -> all pass -> cuts version tag -> PyPI auto-publishes

### cueapi-cli
Claude Code: code change -> open PR -> test CI must pass -> merge
Argus: runs CLI tests -> all pass -> cuts version tag -> PyPI auto-publishes

### Marketing site (cueapi.ai)
Claude Code: code change -> open PR -> CI passes -> merge -> Cloudflare Pages auto-deploys to production
Argus: not involved

### Docs (docs.cueapi.ai)
Claude Code: code change -> open PR -> CI passes -> merge -> Cloudflare Pages auto-deploys
Argus: crawls after deploy -> tests all code examples against real API -> verifies all links -> flags inaccuracies

### Blog (blog.cueapi.ai)
Claude Code: code change -> open PR -> CI passes -> merge -> Cloudflare Pages auto-deploys
Argus: crawls after deploy -> tests all code snippets -> verifies package versions -> flags inaccuracies

## Required CI Checks
cueapi-core: sdk-integration, deploy-staging
cueapi-python: sdk-integration
cueapi-cli: test
No PR review required - CI and Argus are the gates

## Security Rules
1. Never hardcode secrets, API keys, or tokens anywhere
2. Always use environment variables: os.environ.get("SECRET_NAME")
3. Never commit .env files
4. All GitHub Actions must be pinned to commit SHAs not tags
5. If you find a hardcoded secret, remove it and rotate it immediately

## Style Rules
1. No em dashes anywhere in any content
2. No AI-sounding language or corporate speak
3. Short, direct sentences

