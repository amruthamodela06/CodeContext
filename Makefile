.DEFAULT_GOAL := help
.PHONY: help dev test lint eval ingest db-up db-down db-migrate backend-dev

help: ## Show this help
	@echo CodeContext targets:
	@echo   help          Show this help
	@echo   dev           Start backend + frontend + Postgres (Checkpoint E)
	@echo   test          Run all backend tests
	@echo   lint          Run ruff (tsc added in Checkpoint E)
	@echo   eval          Run the evaluation harness (later slice)
	@echo   ingest        Ingest a repo from CLI (Checkpoint C)
	@echo   db-up         Start Postgres + pgvector
	@echo   db-down       Stop the dev database
	@echo   db-migrate    Apply Alembic migrations
	@echo   backend-dev   Run the backend with --reload on 0.0.0.0:8000

## --- User-facing (per CLAUDE.md) ---

dev: ## Start backend + frontend + Postgres together (implemented in Checkpoint E)
	@echo "Not yet implemented. See docs/decisions/ for slice planning."
	@exit 1

test: ## Run all backend tests
	cd backend && uv run pytest

lint: ## Run ruff (tsc added in Checkpoint E)
	cd backend && uv run ruff check . && uv run ruff format --check .

eval: ## Run the evaluation harness (later slice)
	@echo "Not yet implemented."
	@exit 1

ingest: ## Ingest a repo from CLI: make ingest REPO=owner/name (implemented in Checkpoint C)
	@echo "Not yet implemented."
	@exit 1

## --- Building blocks (used during slice 1 dev) ---

db-up: ## Start Postgres + pgvector in the background
	docker compose -f infra/docker-compose.yml up -d

db-down: ## Stop the dev database (data persists in the named volume)
	docker compose -f infra/docker-compose.yml down

db-migrate: ## Apply Alembic migrations
	cd backend && uv run alembic upgrade head

backend-dev: ## Run the backend with --reload on 0.0.0.0:8000
	cd backend && uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
