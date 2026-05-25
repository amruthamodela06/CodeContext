# ADR 0002: POST /ingest is synchronous in Slice 1

**Status**: Accepted
**Date**: 2026-05-25

## Context

Slice 1 introduces `POST /ingest`, which clones a public GitHub repository and stores file metadata (path, size, language) in Postgres. The PRD's progressive-availability goals (§6.1: file structure browsable in 10s, full historical queries in 5min) imply asynchronous, staged ingestion eventually — but Slice 1 has only clone + filesystem walk, no embeddings, no LLM. For small/medium public repos that completes in seconds.

Three options were considered:

1. **Sync, return file list directly** — block until done; respond with result.
2. **Async with `job_id` from day one** — return 202 + `job_id`; introduce a jobs table; add `GET /jobs/{id}` polling.
3. **Sync with SSE progress events** — hold the connection open, stream stage transitions.

## Decision

Slice 1's `POST /ingest` is **synchronous** and returns the full file list in the response body. No job table, no polling endpoint, no streaming.

## Consequences

**Upside**: Simplest possible end-to-end loop. Frontend is a plain `fetch` + render. Tests are direct request/response. Nothing to debug except the actual ingestion logic.

**Cost (known)**: This endpoint will be redesigned in the slice that adds embeddings — embedding 200k LOC through OpenAI's API takes minutes, not seconds. The migration path is known and contained: introduce a `job` table, change this endpoint to "create job, return `job_id`", add `GET /jobs/{id}`. The only consumer of this endpoint is our own frontend, so the blast radius is limited to two files.

**Safeguard**: Reject repos too large to ingest synchronously *before* cloning. PRD §6.1 already caps ingestion at 200k LOC / 5000 files; we enforce that limit via a `git ls-tree` size check before the full clone.

**Not committed by this ADR**: the long-term API shape. A future ADR will document the async migration when we get there.
