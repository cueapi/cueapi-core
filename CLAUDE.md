# CLAUDE.md - Rules for AI agents working on this repo

## Branch protection
- Never bypass branch protection or force merge failing checks
- Never use --no-verify or skip pre-commit hooks
- All changes to main must pass required status checks (sdk-integration, deploy-staging)
- If a check fails, fix the root cause before merging

## Deployment
- Staging deploys automatically on push to main
- Argus tests staging before production promotion
- Never deploy directly to production without Argus passing

## Security
- Never hardcode API keys, secrets, or tokens in source code
- Use environment variables or GitHub secrets for all credentials
- All GitHub Actions must be pinned to commit SHAs, not version tags

## Code quality
- Run tests locally before pushing: pytest tests/ -v
- Do not use em dashes in any user-facing content
- All support/security emails go to support@vector.build
