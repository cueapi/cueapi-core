# Contributing to CueAPI

Thank you for considering contributing to CueAPI.

## Development setup

```bash
git clone https://github.com/govindkavaturi-art/cueapi-core
cd cueapi-core
cp .env.example .env
docker compose up -d db redis
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run the API locally:

```bash
uvicorn app.main:app --reload --port 8000
```

## Running tests

```bash
make test
```

Or without Make:

```bash
docker compose -f docker-compose.test.yml up -d
DATABASE_URL=postgresql+asyncpg://cueapi:cueapi@localhost:5433/cueapi_test \
  REDIS_URL=redis://localhost:6380/0 \
  python -m pytest tests/ -v
```

## Pull requests

1. Fork the repo and create a branch from `main`
2. Add tests for any new functionality
3. Ensure `make test` passes
4. Keep PRs focused — one feature or fix per PR
5. Write clear commit messages

## Code style

- Python 3.11+
- Follow existing patterns in the codebase
- Type hints on function signatures
- Async/await for all database and Redis operations

## Reporting issues

Open an issue on GitHub. Include:

- What you expected to happen
- What actually happened
- Steps to reproduce
- CueAPI version and environment (Docker, bare metal, etc.)

## License

By contributing, you agree that your contributions will be licensed under the Apache 2.0 license.
