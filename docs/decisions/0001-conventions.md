# ADR 0001: Project conventions and workflow

**Status**: Accepted
**Date**: 2026-05-25

## Context

CodeContext is a portfolio project; the *process* of building it (clarity of decisions, defensibility of choices, evaluation rigor) is part of what the artifact demonstrates. We want every non-trivial choice to be discoverable later — both by the author reloading context months from now and by anyone reviewing the project as a hiring signal.

This ADR establishes the baseline conventions every subsequent ADR can assume.

## Decision

### Stack (per PRD §9)

- **Backend**: Python `>=3.12` (currently 3.14 via uv-managed install; floor pin, not exact), FastAPI, SQLAlchemy 2.0 (async style), asyncpg, Alembic, Pydantic v2. Deps managed with `uv`.
- **Frontend**: Next.js 14 (App Router), TypeScript strict, Tailwind. Server components by default; client components only when interactivity is required.
- **Storage**: Postgres 16 + pgvector. Single database for structured rows and vector embeddings.
- **Tooling**: `ruff` for Python lint+format. `tsc` for TypeScript type-check. `pytest` (+ `pytest-asyncio`) for backend tests.

### Workflow

- **Conventional commits**: `feat:`, `fix:`, `chore:`, `docs:`, `test:`, `refactor:`. One slice ≈ one commit or one small PR.
- **ADRs** in `docs/decisions/` for every non-trivial tradeoff. Format: Context / Decision / Consequences. One page max.
- **New dependencies** require a one-line justification — in the commit message that adds them, or in the ADR that prompts them.
- **Tests alongside code**, not after. A slice is not "done" while tests fail.
- **`CLAUDE.md` "current slice" pointer** is updated when a slice closes.

### Escalation

Surface (don't assume) anything that touches: API cost, the public API surface, a new third-party service, an existing test, or the v1 non-goals list in `CLAUDE.md`.

## Consequences

**Upside**: Consistent surface across backend, frontend, and eval. The decision log doubles as interview material — concrete tradeoffs, named alternatives, dated. ADRs prevent re-litigating settled questions per-session.

**Cost**: More upfront ceremony than just shipping code. Some choices (`uv`, `ruff`) are newer / less universal than alternatives (`pip` + `black` + `flake8`); chosen for installer speed and unified config respectively, but a reviewer unfamiliar with them needs the README pointer.

**What this ADR does not lock**: git client library (subprocess vs `gitpython` vs `pygit2`) and frontend package manager (pnpm vs npm vs yarn) — those get their own ADRs when decided.
