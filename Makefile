.DEFAULT_GOAL := help
.PHONY: help dev test lint eval ingest db-up db-down db-migrate backend-dev frontend-dev

help: ## Show this help
	@echo CodeContext targets:
	@echo   help          Show this help
	@echo   dev           Stub — see README for the three-terminal flow
	@echo   test          Run all backend tests
	@echo   lint          Run ruff + tsc
	@echo   eval          Run the evaluation harness (later slice)
	@echo   ingest        Ingest a repo from CLI (later slice)
	@echo   db-up         Start Postgres + pgvector
	@echo   db-down       Stop the dev database
	@echo   db-migrate    Apply Alembic migrations
	@echo   backend-dev   Run the backend with --reload on 0.0.0.0:8000
	@echo   frontend-dev  Run the Next.js dev server on localhost:3000

## --- User-facing (per CLAUDE.md) ---

dev: ## Stub — run db-up, backend-dev, frontend-dev in three terminals (see README)
	@echo "make dev (omnibus) is not wired. Open three terminals and run:"
	@echo "  1) make db-up         (or leave docker-compose running in the background)"
	@echo "  2) make backend-dev"
	@echo "  3) make frontend-dev"
	@echo "Then open http://localhost:3000."

test: ## Run all backend tests
	cd backend && uv run pytest

lint: ## Run ruff (backend) + tsc + eslint (frontend)
	cd backend && uv run ruff check . && uv run ruff format --check .
	cd frontend && pnpm exec tsc --noEmit && pnpm exec eslint .

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

frontend-dev: ## Run the Next.js dev server on localhost:3000
	cd frontend && pnpm dev
