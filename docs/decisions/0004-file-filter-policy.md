# ADR 0004: File filter during ingestion — skip common junk + size cap

**Status**: Accepted
**Date**: 2026-05-25

## Context

A typical Node.js OSS repo can contain hundreds of thousands of files inside `node_modules/`. PRD §6.1 caps ingestion at 5000 files / 200k LOC. We need a filter that keeps real source code in and excludes build artifacts, dependencies, binaries, and large generated files.

Indexing junk wastes storage now and embedding budget later: Slice 2 will turn every indexed file into paid embedding API calls.

## Decision

The ingestion tree walk skips the following, **hardcoded for Slice 1**:

- **Directories** (matched on path component): `.git/`, `node_modules/`, `vendor/`, `.venv/`, `venv/`, `dist/`, `build/`, `__pycache__/`, `target/`, `.next/`, `.nuxt/`, `.cache/`, `.idea/`, `.vscode/`
- **Lockfiles by basename**: `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `Pipfile.lock`, `poetry.lock`, `uv.lock`, `Cargo.lock`, `go.sum`, `composer.lock`, `Gemfile.lock`
- **Files over 1 MiB** (stat-based, before reading contents)
- **Binary extensions**: `.png .jpg .jpeg .gif .svg .ico .webp .pdf .zip .tar .gz .bz2 .7z .exe .dll .so .dylib .o .a .wasm .ttf .otf .woff .woff2 .mp3 .mp4 .mov .webm .ogg .flac .class .jar`

The filter lives as a function in `backend/app/services/ingest.py` (or a sibling module). The skip lists are module-level constants; extending them is a one-line ADR amendment or a follow-up ADR.

## Consequences

**Upside**: Realistic file counts on typical OSS repos — a fresh clone of `tiangolo/fastapi` lands well under the 5000-file ceiling. Embedding budget in later slices stays predictable. The filter is testable against the vendored fixture (ADR 0003), which is built to include one of each junk type.

**Cost**: False negatives. Files in skipped directories are invisible — e.g., docs occasionally live under `build/`, or generated SQL under `target/`. PRD §6.1 provides the escape hatch (specify a subdirectory to ingest). Accepted tradeoff for v1.

**Not configurable in Slice 1**: lists are hardcoded. Becomes per-repo configurable when there's a second user with a different shape of repo, not before. YAGNI.

**Rejected alternative**: Use the repo's own `.gitignore` as the filter. `git clone` already respects `.gitignore` (files matched by it aren't checked in), so honoring it post-clone mostly affects nested ignores and doesn't catch lockfiles or large generated files that ARE checked in — i.e., it doesn't solve our actual problem.
