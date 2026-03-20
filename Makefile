.PHONY: up down test logs migrate shell clean

up:
	docker compose up -d
	@echo ""
	@echo "CueAPI running at http://localhost:8000"
	@echo "API docs: http://localhost:8000/docs"
	@echo ""

down:
	docker compose down

test:
	docker compose -f docker-compose.test.yml up -d --wait
	DATABASE_URL=postgresql+asyncpg://cueapi:password@localhost:5432/cueapi \
	REDIS_URL=redis://localhost:6379 \
	SESSION_SECRET=test-secret-key-32-chars-minimum \
	pytest tests/ -v --tb=short
	docker compose -f docker-compose.test.yml down

migrate:
	docker compose exec api alembic upgrade head

logs:
	docker compose logs -f api poller worker

shell:
	docker compose exec api python

clean:
	docker compose down -v
	docker compose -f docker-compose.test.yml down -v
