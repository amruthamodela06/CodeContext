# ADR 0011: Graph schema (commits / PRs / issues + polymorphic entity_edge)

**Status**: Accepted
**Date**: 2026-06-21

## Context

Slice 5 turns CodeContext from "code search with citations" into "answers grounded in repository history". To do that, retrieval must reach beyond code chunks into the commits, pull requests, and issues that explain *why* the code looks the way it does. PRD §9.5 names this the differentiating capability — every commodity LLM chatbot can read code; almost none can trace "this function exists because of issue #189 that PR #234 closed via commit `a3f5b2c`".

That requires three new things in the data model:

1. **History tables** that mirror GitHub's commits / PRs / issues into the local DB so retrieval and graph traversal never depend on a live GitHub call at query time.
2. **A typed graph** connecting code chunks to the history entities — `chunk → commit → pr → issue` and the inverses.
3. **Resumable ingestion** of the history (a one-year window over a real-world repo is hundreds of paginated GraphQL calls that bump into rate limits).

The graph is where this slice's design surface concentrates. Two structural choices dominate everything else: how to model the edges, and how to populate them.

## Decision

### Domain tables — straightforward mirrors

Five new tables, all keyed by `(repo_id, …)` with appropriate unique constraints so re-running ingestion is an idempotent upsert:

| Table | Natural key | Notable columns |
|---|---|---|
| `commit` | `(repo_id, sha)` | author, dates, message, `parent_shas` JSONB, additions/deletions |
| `pull_request` | `(repo_id, number)` | title, body, state, `merge_commit_sha`, base/head branches |
| `pr_comment` | `(pr_id, github_id)` | author, body, `comment_type` (issue_comment / review_comment / review_body) |
| `issue` | `(repo_id, number)` | title, body, state, JSONB `labels`, `closing_pr_number` (filled by 5c) |
| `issue_comment` | `(issue_id, github_id)` | author, body |

Schema lives in migration `c8d2f5a31e90`. All tables `ON DELETE CASCADE` from `repo` so dropping a repo cleans the history.

### `entity_edge` — single polymorphic edges table

```sql
entity_edge (
  id PK,
  repo_id FK,
  source_type varchar(16),  -- 'chunk' | 'commit' | 'pr' | 'issue'
  source_id   int,
  target_type varchar(16),
  target_id   int,
  edge_type   varchar(32),
  edge_metadata JSONB,
  created_at timestamptz,
  CHECK (edge_type IN ('introduced_by', 'part_of', 'references_issue', 'closed_by')),
  UNIQUE (repo_id, source_type, source_id, target_type, target_id, edge_type),
  INDEX (repo_id, source_type, source_id, edge_type),   -- outbound traversal
  INDEX (repo_id, target_type, target_id, edge_type)    -- inbound traversal
)
```

Four edge types ingested in this slice:
- `chunk -[introduced_by]-> commit` — per-file `git blame --line-porcelain`; the chunk's `start_line` is the canonical attribution point (ADR-noted limitation).
- `commit -[part_of]-> pr` — `PullRequest.merge_commit_sha` joined to a `Commit` row.
- `pr -[references_issue]-> issue` — PR title/body parsed for `fixes|closes|resolves #N` (bare `#N` mentions ignored — too noisy).
- `issue -[closed_by]-> pr` — inverse of `references_issue`, fires only when the PR is merged. Also populates `Issue.closing_pr_number`.

The `chunk -[in_file]-> file` relationship is **not** in `entity_edge` — `code_chunk.file_id` is already an FK; duplicating it as an edge adds writes without expressivity. The graph table is for *cross-domain* relationships only.

**`edge_metadata`** (not `metadata` — SQLAlchemy reserves that on `DeclarativeBase`) carries per-edge hints: `blame_line` for introduced_by, future fields per edge type as eval surfaces a need.

### History ingestion — separate endpoint, resumable cursor

`POST /repos/{repo_id}/ingest-history` mirrors the embed pattern: 202 + BackgroundTask, status polled at `GET /repos/{repo_id}/history-ingestion-status`. Reasons it's separate from `POST /ingest`:

- Code ingest is fast (seconds-minutes); history is rate-limit-bound (asyncer's 460 commits + 287 PRs took ~26s with `5000-pt/hr` GraphQL quota intact, and could be 30+ min for a large repo whose first call burns the quota).
- Code-only is a valid UX state — the user can ingest + embed + ask code-Q&A immediately; history fills in later.
- The background job needs to be resumable — a daemon restart or rate-limit pause continues from the persisted cursor in `repo.history_ingestion_state`, not from scratch.

State shape (per-stage cursor + count + done flag, plus a window cutoff):

```json
{
  "window_since": "2025-06-21T00:00:00+00:00",
  "started_at":  "2026-06-21T00:00:00+00:00",
  "commits":       {"cursor": "...", "count": 245, "done": true},
  "pull_requests": {"cursor": "...", "count": 120, "done": false},
  "issues":        {"cursor": null,   "count": 0,   "done": false}
}
```

The orchestrator (`app/history/orchestrator.py`) runs three stages sequentially (commits / PRs / issues), persisting per-stage state after every page. Each stage is idempotent via Postgres `ON CONFLICT DO UPDATE` (`upsert_commits` / `upsert_pull_requests` / `upsert_issues`).

### Why GraphQL (not REST)

GitHub's GraphQL endpoint is the only sane way to fetch nested PR comments + review bodies in one round-trip per page. REST would require 1 list call + N comment calls per PR. GraphQL costs are predictable (`rateLimit { cost limit remaining resetAt }` surfaces on every response); the client (`app/history/client.py`) pauses internally when `remaining < 50` and resumes at `resetAt`.

### Per-file blame, not per-chunk

The naive blame strategy is one `git blame` invocation per chunk. For a 5000-file repo with thousands of chunks, that's wall-clock pain. The right pattern is **one `git blame --line-porcelain` per file**, parse once, look up each chunk's `start_line` in the resulting map. Single SHA per chunk by design (the `start_line` attribution rule documented as a v1 limitation — a chunk that spans multiple commits gets attributed to whoever wrote line one).

### Stub commits for blame SHAs outside the window

Blame may return a SHA older than the GraphQL 12-month window — the `commit` row doesn't exist, so the `introduced_by` edge has nowhere to point. Solution: when an unknown SHA appears, run `git log -1 --format='%an%x00%ae%x00%aI%x00%cI%x00%B'` against the local clone and insert a minimal `commit` row from that output. `ON CONFLICT DO NOTHING` ensures a real GraphQL row never gets overwritten by a stub.

Asyncer's "Add main initial core code" commit from 2022-01-04 (well outside the 12-month window) was stub-inserted this way during the live verification, and the `chunk → commit` chain reaches back to it correctly.

## Consequences

**Upside**

- Single polymorphic edges table lets retrieval traverse cross-domain chains with one recursive CTE (Slice 5f) instead of N UNIONs across typed edge tables. Adding a new edge type is `ALTER … CHECK` + ingester code, no schema migration per type.
- Outbound + inbound indexes mean reverse-traversal ("what chunks does this commit introduce?") is the same cost as forward.
- History ingestion is resumable across restarts; rate-limit pauses don't lose work.
- `ON CONFLICT DO UPDATE` upserts let re-running ingestion serve as a refresh path — same endpoint handles initial + incremental.
- Stub commits guarantee chunks always attribute to *some* commit, even for code older than the ingestion window. Multi-hop never has a "missing target" gap to apologize for.

**Costs / limitations**

- Lost FK enforcement on `entity_edge.source_id` / `target_id` — the polymorphic shape means we can't constrain those to a specific table. Mitigation: ingestion is the only writer, runs transactionally, and the `CHECK` on `edge_type` plus the `UNIQUE` constraint catch the most common ingester bugs.
- Blame attribution is single-line (chunk's `start_line`). A function whose original lines are spread across multiple commits gets attributed to whoever wrote the signature. Documented; revisit if Slice 7 eval surfaces wrong attributions hurting answer quality.
- Window is symmetric per stage (`since: window_start` for commits, client-side cutoff for PRs/issues by `updatedAt`). A merged PR from outside the window whose `merge_commit_sha` is *inside* the window will create a `part_of` edge from commit to a non-existent PR row. Currently unhandled (drops the edge silently in 5c's `_stage_part_of`); fix by extending stub-insertion to PRs if it ever matters.
- `pr_comment` covers `issue_comment` (general thread) + `review_body` (top-level review text); inline review-thread comments (`reviewThreads`) are out of scope for v1 — adds a per-thread per-line pagination dimension and rarely changes answer quality.

**Rejected alternatives**

- *Separate edge tables per type* (`chunk_commit_edges`, `commit_pr_edges`, …) — type-safe via FKs, but doubles the schema surface, makes multi-hop traversal a UNION across N tables, and turns "add a new edge type" into a migration. Polymorphic + CHECK is the cleaner tradeoff.
- *Materialize `chunk -> file` as an entity_edge row* — `code_chunk.file_id` is already an FK; duplicating it just adds writes.
- *Single `/ingest` endpoint that does code + history* — code ingest stays synchronous, history needs background + resumability. Conflating them means either the synchronous path becomes 30+ min wait, or both share a problematic mid-tier status model. Separate is cleaner.
- *pygit2 (libgit2 bindings) for blame* — ~2× faster than subprocess but adds a non-trivial build dep. Background-job latency isn't the bottleneck; subprocess is fine.

**Re-evaluation triggers**

- If a fifth or sixth edge type appears and edge_type-specific querying becomes hot (joins, group-bys), revisit the polymorphic vs typed-tables decision.
- If the blame single-line-attribution rule hurts the eval's `historical_why` accuracy, upgrade to "most-frequent commit across the chunk's line range".
- If pre-indexed popular repos (PRD §6.5) push ingestion volume past free-tier GraphQL quota at scale, layer a queue + worker pool above the per-repo background-task model.
