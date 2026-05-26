# CodeContext

Ask natural-language questions about any public GitHub repository. Get cited answers grounded in the repo's code, commits, PRs, and issues.

This is a v1 portfolio project under active development. See [docs/PRD.md](docs/PRD.md) for the full product spec and [CLAUDE.md](CLAUDE.md) for working conventions.

## Status

**Slice 1 (current):** repo ingestion + file list — backend (`POST /ingest`, `GET /repos/{owner}/{name}/files`) and a minimal Next.js UI. No embeddings, no RAG, no LLM yet.

## Quick start

Prereqs (Windows; install paths in parens): **docker** (Desktop), **uv** (`winget install astral-sh.uv`), **GNU make** (`winget install ezwinports.make`), **Node ≥18** (`winget install OpenJS.NodeJS.LTS`), **pnpm** (`npm install -g pnpm`). After a fresh install, restart your terminal (or VSCode) so the new tools are on PATH.

One-time per clone:

```bash
cp .env.example .env
make db-up && make db-migrate
cd backend && uv sync     # creates the backend venv, downloads Python 3.12+
cd ../frontend && pnpm install
```

Day-to-day (three terminals — `make dev` is intentionally not wired yet, see [Makefile](Makefile)):

```bash
make db-up         # terminal 1 — Postgres + pgvector (or leave running in the background)
make backend-dev   # terminal 2 — FastAPI on http://localhost:8000
make frontend-dev  # terminal 3 — Next.js on http://localhost:3000
```

Other targets:

```bash
make test     # 56 backend tests (uses an isolated codecontext_test DB)
make lint     # ruff (backend) + tsc + eslint (frontend)
make eval     # evaluation harness (Slice 2)
make ingest REPO=owner/name   # CLI ingestion (Slice 2)
```

## Architecture

- **Backend**: Python 3.12, FastAPI, SQLAlchemy 2.0 (async), Alembic, `uv`
- **Storage**: Postgres 16 + pgvector (single DB for structured + vector data)
- **Frontend**: Next.js 14 (App Router), TypeScript strict, Tailwind
- **Eval**: pytest-based harness in `eval/`, datasets versioned alongside

## Documents

- [docs/PRD.md](docs/PRD.md) — product spec
- [docs/decisions/](docs/decisions/) — architecture decision records (ADRs)
- [CLAUDE.md](CLAUDE.md) — conventions and working style
