# CodeContext

Ask natural-language questions about any public GitHub repository. Get cited answers grounded in the repo's code, commits, PRs, and issues.

This is a v1 portfolio project under active development. See [docs/PRD.md](docs/PRD.md) for the full product spec and [CLAUDE.md](CLAUDE.md) for working conventions.

## Status

**Slice 1 (current):** repo ingestion + file list. No embeddings, no RAG, no LLM yet.

## Quick start

Quick-start instructions will be filled in as Slice 1 services come online. The intended interface (see [CLAUDE.md](CLAUDE.md) §"Common commands") is:

```bash
make dev      # start backend + frontend + Postgres
make test     # run all tests
make lint     # ruff + tsc
make eval     # evaluation harness (later slice)
make ingest REPO=owner/name   # ingest a repo from the CLI
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
