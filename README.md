# CodeContext

Ask natural-language questions about any public GitHub repository and get cited answers grounded in the repo's code, commits, PRs, and issues.

This is a v1 portfolio project under active development. See [docs/PRD.md](docs/PRD.md) for the full product spec, [docs/roadmap.md](docs/roadmap.md) for what's deferred to v1.1/v2+, and [CLAUDE.md](CLAUDE.md) for working conventions.

## Status

Multi-hop retrieval works end-to-end: a `historical_why` question routes through the typed graph (chunk → commit → PR → issue) and the LLM cites every step with a clickable typed chip.

| Slice | What it added | State |
|---|---|---|
| 1 | Repo ingestion + file list | ✅ done |
| 2 | AST chunking (tree-sitter, Python) | ✅ done |
| 3 | Embeddings + naive vector search | ✅ done |
| 4 | LLM answers with mechanically-verified citations (streaming) | ✅ done |
| 5 | Multi-hop graph retrieval + typed citations (commits / PRs / issues) | ✅ done |
| 6 | Hybrid retrieval (BM25 + vector, RRF) + cross-encoder reranker | ⏳ next |

**What works today**: ingest a public GitHub repo → AST-chunk + embed → ingest 12 months of commits / PRs / issues via GraphQL → build the `chunk → commit → pr → issue` graph via blame + PR-body parsing → ask a question. The classifier (keyword default, LLM opt-in) routes the query; `historical_why` triggers multi-hop expansion + embedding rerank; the LLM streams an answer with typed citation chips (`[chunk:cN]` / `[commit:mN]` / `[pr:pN]` / `[issue:iN]`), each clickable to a per-type viewer with an "Open on GitHub" link.

Demonstrated on `tiangolo/asyncer`: 100 files + 132 chunks + 460 commits + 287 PRs + 1 issue ingested. Asking *"why was syncify added?"* routes to `historical_why` (keyword classifier, 90% confidence), expands the seed chunks via the graph, reranks to 3 commits, and produces a cited answer like *"`syncify` was added to allow synchronous execution of asynchronous functions … This feature was introduced in `[commit:m1]` and further expanded with tests and documentation in `[commit:m2]` and `[commit:m3]`. The initial implementation of `syncify` was done in `[chunk:c1]`."* — with `commit:m1` resolved to `/commit/6a713b0…` (a 2022 commit stub-inserted by blame, four years outside the GraphQL window) and `chunk:c1` resolved to the SHA-pinned blob URL at `asyncer/_main.py:244-312`.

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
| `POST /repos/{repo_id}/embed` | Embed all entities (chunks + commits + PRs + issues; background job; 202) |
| `GET /repos/{repo_id}/embedding-status` | Poll embedding progress (per-type counts) |
| `POST /repos/{repo_id}/ingest-history` | Background: fetch commits / PRs / issues via GraphQL (resumable; requires `GITHUB_TOKEN`) |
| `GET /repos/{repo_id}/history-ingestion-status` | Poll history ingestion progress |
| `POST /repos/{repo_id}/build-graph` | Background: per-file blame + PR-body parsing → `entity_edge` rows |
| `GET /repos/{repo_id}/graph-status` | Poll graph-build progress (per-edge-type counts) |
| `POST /search` | Naive cosine search → top-k chunks with similarity |
| `POST /query` | Ask a question → classify → retrieve → SSE stream of answer tokens + typed citations + debug trace |
| `GET /healthz` | Liveness |

`POST /query` streams Server-Sent Events: `sources` (typed dict: chunks / commits / PRs / issues, each carrying its own permalink) → `token`×N (answer deltas) → `citations` (typed `ResolvedCitation`s + warnings + classifier/multi-hop trace) → `done`, or `error` on mid-stream failure. The browser consumes it with `fetch` + `ReadableStream` (it's a POST, so not `EventSource`).

## Architecture

- **Backend**: Python ≥3.12 (currently 3.14), FastAPI, SQLAlchemy 2.0 (async), Alembic, `uv`
- **Storage**: Postgres 16 + pgvector — single DB for structured rows *and* embedding vectors
- **Parsing**: tree-sitter (`tree-sitter-language-pack`) — Python implemented; TS/JS/Go/Rust stubbed
- **Embeddings**: `bge-small-en-v1.5` (384-dim) via `sentence-transformers`, CPU, in-process; behind a swappable `Embedder` interface (`EMBEDDING_PROVIDER` env)
- **Vector index**: pgvector HNSW (cosine), built after bulk insert
- **LLM**: Gemini 2.0 Flash (free tier) by default / Ollama Qwen 2.5 Coder 3B/7B offline, behind a swappable `LLMProvider` interface (`LLM_PROVIDER` env); one OpenAI-SDK transport for both (ADR 0007)
- **History**: commits / PRs / issues mirrored via GitHub GraphQL into local tables; per-file `git blame` + PR-body parsing populates a polymorphic `entity_edge` graph (ADR 0011)
- **Query classifier**: keyword default (sub-ms) or LLM opt-in (`QUERY_CLASSIFIER` env); routes to flat vs. multi-hop retrieval (ADR 0012)
- **Multi-hop retrieval**: recursive-CTE traversal over `entity_edge` (depth 2 / breadth 10) + embedding rerank of expanded set; only for `historical_why` queries (ADR 0012)
- **Citations**: typed `[chunk:cN]` / `[commit:mN]` / `[pr:pN]` / `[issue:iN]`, parsed (code-fence-aware, shape-only), validated against the retrieved set, resolved to per-type permalinks (ADR 0010 + 0012)
- **Frontend**: Next.js 16 (App Router), React 19, TypeScript strict, Tailwind 4; Monaco for cited-chunk rendering; per-type Sources panels + chip viewers
- **Eval**: pytest-based harness in `eval/` (Slice 7)

All ML runs on CPU — no GPU assumed (see [ADR 0007](docs/decisions/0007-oss-embeddings-llm-provider-abstraction.md)). The default path uses free/local providers; paid APIs are for ablation only.

## Repository layout

```
backend/    FastAPI app, SQLAlchemy models, Alembic migrations, pytest suite
frontend/   Next.js App Router UI
infra/      docker-compose (Postgres + pgvector)
docs/       PRD, roadmap, and decisions/ (ADRs 0001–0013)
eval/       evaluation harness (later slice)
```

## Documents

- [docs/PRD.md](docs/PRD.md) — product spec (v2)
- [docs/roadmap.md](docs/roadmap.md) — v1.1 / v2 / v3+ and explicit non-goals
- [docs/decisions/](docs/decisions/) — architecture decision records (ADRs)
- [CLAUDE.md](CLAUDE.md) — conventions and working style
