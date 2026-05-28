# CodeContext — Conversational Code Intelligence for GitHub Repos

A web app that lets developers ask natural-language questions about public GitHub repositories and get cited answers grounded in the repo's code, commit history, PRs, and issues.

This is a portfolio project. **Rigor and quality matter more than feature count.** Every choice should be defensible in an interview.

---

## Status

- **Phase**: v1 development
- **Current slice**: Slice 1 complete — ingestion + file list, end-to-end through the Next.js UI. Slice 2 TBD (PRD §11 week 2 is AST chunking + hybrid retrieval + citations).
- **Flagship eval repo**: FastAPI (`tiangolo/fastapi`)

---

## Project layout

```
backend/        Python >=3.12 (currently 3.14) + FastAPI. uv for deps.
frontend/      Next.js >=14 (currently 16) + React 19 + TS + Tailwind 4. App Router. pnpm.
eval/          Evaluation harness and datasets. Separate from app code.
infra/         docker-compose for local dev (Postgres + pgvector).
docs/          PRD, architecture notes, decisions/ (ADRs).
```

---

## Conventions

### Backend (Python)
- Type hints everywhere. No untyped functions.
- Pydantic v2 for schemas. SQLAlchemy 2.0 style for ORM.
- `ruff` for lint + format. `pytest` for tests. `pytest-asyncio` for async.
- Async by default for IO. Sync only when justified.
- One module = one responsibility. If a file exceeds ~300 lines, consider splitting.

### Frontend (TypeScript)
- Strict TypeScript. No `any` without an inline comment justifying it.
- Server components by default. Client components only when interactivity is required.
- Tailwind for styling. No CSS-in-JS, no styled-components.
- Monaco for code rendering. Shiki acceptable for static highlighting.

### Git
- Conventional commits: `feat:`, `fix:`, `chore:`, `docs:`, `test:`, `refactor:`.
- One slice = one commit (or one small PR). Slices are defined per-prompt.
- Commit messages describe *why*, not just *what*.

### Decisions
- Every non-obvious tradeoff goes in `docs/decisions/` as a short ADR.
- ADR format: Context, Decision, Consequences. Keep it to one page.
- New dependencies require a one-line justification in the commit message.

---

## Working style

**Before writing code:**
- For any task larger than ~50 lines, propose a plan first. Wait for approval.
- When a decision has non-obvious tradeoffs, stop and ask. Don't guess.
- Read the existing code before adding new code. Match its style.

**While writing code:**
- Tests alongside code, not after.
- Prefer the stdlib or existing deps. Don't add libraries casually.
- YAGNI: build for the current slice, not for speculative future needs.
- Working > fast > pretty, in that order, for v1.

**After writing code:**
- Run the test suite. Don't declare a slice done while tests fail.
- Update `CLAUDE.md` "current slice" pointer when a slice closes.
- If a non-obvious decision was made, write an ADR.

---

## Scope guardrails

### In scope for v1
- Public GitHub repos only
- Single repo per session
- Web UI (desktop-first)
- Hybrid retrieval (BM25 + vector) with citation-required answers
- Quantitative evaluation on at least one flagship repo

### Out of scope for v1 — do not build
- Private repo support
- Code generation, editing, or PR drafting
- Real-time index updates (manual refresh only)
- Cross-repo or multi-repo queries
- IDE integration
- Mobile-optimized UI
- Authentication beyond GitHub OAuth (and even that comes in a later slice)

If a request seems to push past these guardrails, flag it and ask before proceeding.

---

## Key technical choices (locked for v1)

These are settled. Don't re-litigate without a written ADR.

- **Python runtime**: `>=3.12` (currently 3.14 via `uv`). Floor pin, not exact; uv lockfile + `.python-version` give reproducibility.
- **Database**: Postgres + pgvector (single DB for structured + vector data)
- **Parsing**: tree-sitter for AST extraction
- **Embeddings**: `bge-small-en-v1.5` (384-dim) via `sentence-transformers` (CPU, in-process) by default. Pluggable via an `Embedder` interface; `bge-base-en-v1.5`, OpenAI `text-embedding-3-small`, Voyage `voyage-code-2`, and Ollama `nomic-embed-text` are alternative implementations used in ablation. See ADR 0007 (provider abstraction) + ADR 0009 (bge-small default + vector storage).
- **LLM**: Gemini 2.0 Flash (free tier) by default, behind an `LLMProvider` interface. Implementations: `gemini`, `openai` (GPT-4o-mini), `anthropic` (Claude Haiku/Sonnet), `ollama` (Qwen 2.5 Coder 3B). Selected via the `LLM_PROVIDER` env. All providers must support streaming. See ADR 0007.
- **Retrieval**: Hybrid (BM25 via Postgres FTS + vector via pgvector) with reciprocal rank fusion
- **Languages supported**: Python, TypeScript/JavaScript, Go, Rust (in that priority order)

---

## Evaluation is a first-class deliverable

Every retrieval or generation change must be evaluated. The eval set lives in `eval/`. Tracked metrics:

- Retrieval recall@5, recall@10
- Citation accuracy (% of citations pointing to real, relevant locations)
- Answer quality (LLM-as-judge, with hand-checked sample)
- p50 / p95 latency
- Cost per query

When in doubt about a design choice: run the ablation, report the delta.

---

## Common commands

```bash
make dev            # start backend + frontend + Postgres
make test           # run all tests
make lint           # run ruff + tsc
make eval           # run the evaluation harness against the current build
make ingest REPO=…  # ingest a specific repo from CLI
```

(These should exist in the Makefile. If they don't yet, add them as part of the current slice.)

---

## Things to ask me about, not assume

- Anything that touches cost (API spend, infra, storage)
- Anything that **assumes GPU availability** — dev box is CPU-only (Ryzen 5 7535HS, integrated Radeon 660M, 16 GB RAM). See PRD §9 dev-env notes.
- Anything that **escapes the free-tier default providers** for the deployed path (i.e., escalates to a paid OpenAI/Anthropic/Voyage API for the default `LLM_PROVIDER` or `EMBEDDING_PROVIDER`). Paid providers are for ablation only. See ADR 0007.
- Anything that changes the public API surface
- Anything that requires a new third-party service
- Anything that breaks an existing test
- Anything in the "out of scope for v1" list above
