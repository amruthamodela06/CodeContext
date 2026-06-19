# CodeContext

Ask natural-language questions about any public GitHub repository and get cited answers grounded in the repo's code, commits, PRs, and issues.

This is a v1 portfolio project under active development. See [docs/PRD.md](docs/PRD.md) for the full product spec, [docs/roadmap.md](docs/roadmap.md) for what's deferred to v1.1/v2+, and [CLAUDE.md](CLAUDE.md) for working conventions.

## Status

Naive RAG is end-to-end: ingest → chunk → embed → retrieve → **cited, streaming LLM answer**. Retrieval-quality work (hybrid + rerank) is next.

| Slice | What it added | State |
|---|---|---|
| 1 | Repo ingestion + file list | ✅ done |
| 2 | AST chunking (tree-sitter, Python) | ✅ done |
| 3 | Embeddings + naive vector search | ✅ done |
| 4 | LLM answers with mechanically-verified citations (streaming) | ✅ done |
| 5 | Hybrid retrieval (BM25 + vector, RRF) + reranker | ⏳ next |

**What works today**: paste a public GitHub URL → it shallow-clones, walks + filters files, AST-chunks the Python ones, embeds each chunk locally on CPU (`bge-small-en-v1.5`), stores vectors in pgvector, then **answers natural-language questions with a streaming LLM response grounded in the retrieved code**. Every factual claim carries a `[chunk:cN]` citation that's mechanically verified against the retrieved set and resolved to a commit-SHA-pinned GitHub permalink; a Sources panel shows everything that was retrieved. Raw semantic search is still available too.

Demonstrated on `tiangolo/asyncer`: 100 files → 132 chunks → asking *"how do I run an async function from synchronous code?"* returns a cited answer pointing at `syncify` / `runnify`, each citation clickable to the exact lines on GitHub.

## Quick start

Prereqs (Windows; install commands in parens): **docker** (Desktop), **uv** (`winget install astral-sh.uv`), **GNU make** (`winget install ezwinports.make`), **Node ≥18** (`winget install OpenJS.NodeJS.LTS`), **pnpm** (`npm install -g pnpm`). After a fresh install, restart your terminal (or VSCode) so the new tools are on PATH.

One-time per clone:

```bash
cp .env.example .env
make db-up && make db-migrate
cd backend && uv sync       # creates the backend venv (Python 3.12+ via uv)
cd ../frontend && pnpm install
```

> **LLM key for answers**: `/query` uses Gemini 2.0 Flash by default — add a free `GEMINI_API_KEY` (from [Google AI Studio](https://aistudio.google.com/)) to `.env`. To run fully offline instead, set `LLM_PROVIDER=ollama`, then `ollama pull qwen2.5-coder:3b-instruct`. Ingestion, chunking, embeddings, and search need no LLM key. Gemini's free tier is rate-limited (~15 req/min, guarded by `GEMINI_RPM_LIMIT`).

Day-to-day (three terminals — `make dev` is intentionally not wired yet, see [Makefile](Makefile)):

```bash
make db-up         # terminal 1 — Postgres + pgvector (or leave running in the background)
make backend-dev   # terminal 2 — FastAPI on http://localhost:8000
make frontend-dev  # terminal 3 — Next.js on http://localhost:3000
```

Then open http://localhost:3000, ingest a repo, click **Generate embeddings**, and search.

> The first embed downloads the `bge-small-en-v1.5` model (~130 MB) to your HuggingFace cache, then embeds on CPU. A large repo can take several minutes — the UI shows live progress.

Other targets:

```bash
make test     # backend test suite (isolated codecontext_test DB; fake embedder)
make lint     # ruff (backend) + tsc + eslint (frontend)
make eval     # evaluation harness (later slice — stub)
make ingest REPO=owner/name   # CLI ingestion (later slice — stub)
```

The real embedding model is exercised by one slow test, off by default:

```bash
cd backend && RUN_SLOW=1 uv run pytest -k bge_small   # downloads + runs the real model
```

## API

| Method + path | Purpose |
|---|---|
| `POST /ingest` | Clone a public repo, store file metadata, auto-chunk |
| `GET /repos/{owner}/{name}/files` | Indexed file list with per-file chunk counts |
| `POST /repos/{repo_id}/chunk` | Re-chunk a repo (idempotent) |
| `GET /repos/{repo_id}/chunks` | List chunks (paginated; filter by type / language / file) |
| `GET /chunks/{chunk_id}` | Fetch one chunk |
| `POST /repos/{repo_id}/embed` | Embed all chunks (background job; 202) |
| `GET /repos/{repo_id}/embedding-status` | Poll embedding progress |
| `POST /search` | Naive cosine search → top-k chunks with similarity |
| `POST /query` | Ask a question → SSE stream of answer tokens + verified citations |
| `GET /healthz` | Liveness |

`POST /query` streams Server-Sent Events: `sources` (all retrieved chunks) → `token`×N (answer deltas) → `citations` (resolved + warnings) → `done`, or `error` on mid-stream failure. The browser consumes it with `fetch` + `ReadableStream` (it's a POST, so not `EventSource`).

## Architecture

- **Backend**: Python ≥3.12 (currently 3.14), FastAPI, SQLAlchemy 2.0 (async), Alembic, `uv`
- **Storage**: Postgres 16 + pgvector — single DB for structured rows *and* embedding vectors
- **Parsing**: tree-sitter (`tree-sitter-language-pack`) — Python implemented; TS/JS/Go/Rust stubbed
- **Embeddings**: `bge-small-en-v1.5` (384-dim) via `sentence-transformers`, CPU, in-process; behind a swappable `Embedder` interface (`EMBEDDING_PROVIDER` env)
- **Vector index**: pgvector HNSW (cosine), built after bulk insert
- **LLM**: Gemini 2.0 Flash (free tier) by default / Ollama Qwen 2.5 Coder 3B offline, behind a swappable `LLMProvider` interface (`LLM_PROVIDER` env); one OpenAI-SDK transport for both (ADR 0007)
- **Citations**: per-query `[chunk:cN]` IDs, parsed (code-fence-aware, shape-only) and validated against the retrieved set, resolved to commit-SHA-pinned permalinks (ADR 0010)
- **Frontend**: Next.js 16 (App Router), React 19, TypeScript strict, Tailwind 4; Monaco for cited-chunk rendering
- **Eval**: pytest-based harness in `eval/` (later slice)

All ML runs on CPU — no GPU assumed (see [ADR 0007](docs/decisions/0007-oss-embeddings-llm-provider-abstraction.md)). The default path uses free/local providers; paid APIs are for ablation only.

## Repository layout

```
backend/    FastAPI app, SQLAlchemy models, Alembic migrations, pytest suite
frontend/   Next.js App Router UI
infra/      docker-compose (Postgres + pgvector)
docs/       PRD, roadmap, and decisions/ (ADRs 0001–0010)
eval/       evaluation harness (later slice)
```

## Documents

- [docs/PRD.md](docs/PRD.md) — product spec (v2)
- [docs/roadmap.md](docs/roadmap.md) — v1.1 / v2 / v3+ and explicit non-goals
- [docs/decisions/](docs/decisions/) — architecture decision records (ADRs)
- [CLAUDE.md](CLAUDE.md) — conventions and working style
