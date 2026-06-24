# ADR 0012: Query classifier + multi-hop graph retrieval

**Status**: Accepted
**Date**: 2026-06-22

## Context

Slice 4 retrieval is flat — every `/query` runs the same top-k cosine search over chunk embeddings and stuffs the results into one prompt. That's fine for "what does this function do?" and "where is the JWT validator?". It breaks on "why was bcrypt added?" — the chunk that *implements* bcrypt doesn't carry the rationale. The rationale lives in the commit message, the PR body, the issue thread.

Slice 5 adds two layers on top of Slice 4's flat path:

1. A **query classifier** that routes each query into a category (lookup / architectural / historical_why / impact / out_of_scope) so the system picks the right retrieval strategy.
2. **Multi-hop graph retrieval** for `historical_why` queries — start from the top-k chunks, walk `entity_edge` to commits / PRs / issues, embedding-rerank the expanded set, and pass everything to the LLM with a prompt that teaches it to trace the chain.

Both layers fit into the existing Slice 4 contract: same `POST /query` endpoint, same SSE stream, same parse-validate-resolve citation discipline. The differences are entirely *what gets into the prompt* and *how the response surfaces routing decisions*.

This ADR documents the two layers + their wire shape.

## Decision

### Five categories — fixed taxonomy for v1

```python
Category = Literal["lookup", "architectural", "historical_why", "impact", "out_of_scope"]
```

Picked because they each map to a distinct retrieval strategy:

| Category | Example | Retrieval |
|---|---|---|
| `lookup` | "Where is the JWT validator?" | Flat top-k chunks. |
| `architectural` | "How does the middleware chain work?" | Flat top-k chunks (system-level understanding from code alone). |
| `historical_why` | "Why was bcrypt added?" | Flat chunks + multi-hop expansion + rerank. |
| `impact` | "What calls `validate_user_input`?" | Flat top-k chunks (Slice 6 will add real dependency analysis). |
| `out_of_scope` | "Best pasta recipe?" | Short-circuit — no retrieval, no LLM, canned response. |

Slice 7 eval will be the arbiter of whether the taxonomy needs splitting (e.g., `impact` vs `dependency-graph`) or merging (e.g., `lookup` ⊆ `architectural`).

### Two classifier implementations behind one interface

`QueryClassifier` ABC + factory keyed on `QUERY_CLASSIFIER` env var (mirrors `Embedder` / `LLMProvider` from Slices 3 / 4):

- **`keyword`** (default) — pure-Python regex matching in priority order. Sub-ms, no LLM call. `historical_why` patterns matched first (most specific, most expensive to misroute). A query with no pattern match falls back to `lookup` with confidence `0.3` so the /query router can treat it as low-signal.
- **`llm`** (opt-in) — one extra LLM call per query using the existing `LLMProvider`. Strict JSON output (`{"category": "...", "confidence": 0.0-1.0}`); parse failure or unknown category falls back to keyword internally and flags `fallback_used=True` so /query sees the auto-recovery.

`ClassificationResult` carries `category + confidence + method + fallback_used`. The routing layer reads all four — confidence below 0.6 OR fallback_used both signal "low trust", which (per the §6.2 graceful-degradation pattern from the PRD) means the /query router can fall back to running BOTH pipelines and merging. Currently /query just routes on category; the dual-pipeline merge is hooked but not enabled until Slice 7 eval shows the keyword classifier's confidence threshold is well-calibrated.

**Keyword is default**: latency wins. The LLM classifier adds ~200–500ms to every query; for "where is X?" that's a regressive trade. The LLM classifier is on the opt-in path for ablation in Slice 7.

### Multi-hop traversal — recursive CTE, depth 2, breadth 10

`app/graph/multihop.py::traverse_outbound` walks `entity_edge` from a seed chunk set:

```sql
WITH RECURSIVE expansion AS (
  -- Anchor: seed chunks at depth 0, type-cast for recursive-CTE column type match.
  SELECT seed.id AS origin_chunk, 'chunk'::varchar(16) AS entity_type, seed.id AS entity_id, 0 AS depth
  FROM unnest(:seed_ids) AS seed(id)
  UNION ALL
  -- Recursive: follow outbound entity_edge rows to depth N.
  SELECT e.origin_chunk, ee.target_type, ee.target_id, e.depth + 1
  FROM expansion e JOIN entity_edge ee ON ...
  WHERE e.depth < :max_depth
),
-- ROW_NUMBER caps fan-out per seed (breadth limit).
ranked AS (... ROW_NUMBER() OVER (PARTITION BY origin_chunk ORDER BY depth, entity_id) AS rn FROM dedup)
SELECT DISTINCT entity_type, entity_id, MIN(depth) AS depth
FROM ranked WHERE rn <= :max_breadth GROUP BY entity_type, entity_id
```

**Depth 2** by default = reaches `chunk → commit → pr`. Does *not* reach issues at hop 3. Issues arrive via flat retrieval when the question's embedding matches an issue body, or via the inverse `closed_by` edge when an issue closes a PR that the chain already includes. The 2-vs-3 tradeoff: each extra hop multiplies the candidate set into rerank; deeper expansion is a Slice 7 tuning concern.

**Breadth 10** per seed = a single seed chunk that fans out to 100 commits gets capped at 10 (most-recent / lowest-id-tie-break). With top_k=5 seeds, max 50 expansion candidates to rerank — cheap.

### Reranker — cosine similarity on `entity_embedding`

The Slice 5d-widened `entity_embedding` table holds vectors for all four entity types. `rerank_by_embedding` filters to the expanded `(entity_type, entity_id)` set, computes cosine distance against the query vector, returns the top-k sorted by similarity. Candidates without an embedding (stub commits with empty messages) are silently dropped — can't rank what we can't embed.

No cross-encoder reranker in v1; deferred to Slice 6's hybrid + reranker work.

### `historical_why` prompt — teaches the chain

Separate prompt template (`build_historical_why_messages`) for the `historical_why` route. Key differences from the Slice 4 base prompt:

- Teaches four typed citation tokens: `[chunk:cN]` / `[commit:mN]` / `[pr:pN]` / `[issue:iN]`.
- Explicit rule + example for chain-tracing: *"When the excerpts form a chain (issue → PR → commit → code), trace it explicitly: e.g. 'the audit flagged the MD5 fallback [issue:i4], so the team replaced it [chunk:c1] in [commit:m2] via [pr:p3]'"*.
- Relationship phrases (`introduced [c1]`, `contains [m2]`, `closed by [p3]`) embedded in each excerpt header so the model sees the typed-entity graph structure inline without us having to verbally explain it.

The non-`historical_why` categories continue to use the Slice 4 single-token prompt — the typed-token vocabulary would be wasted on a flat chunks-only context.

### Out-of-scope short-circuit

`category == "out_of_scope"` skips retrieval AND the LLM call entirely. Returns a canned response: *"I don't see anything in this repo's code or history that relates to your question…"*. Saves the LLM round-trip (cost + latency) and avoids feeding adversarial / unrelated queries through the same prompt chain as legitimate questions.

Defense in depth: the §6.2 *retrieval-confidence* check (low-similarity top-k → "I don't have enough information") still runs after retrieval for in-scope-phrased queries that simply have no matching code. The two checks catch different failure modes.

### Wire shape — `POST /query` debug surface

`QueryRequest` gains `classification_override` (any Category literal) for testing — bypasses the classifier entirely.

`QueryResponse` (and the SSE `citations` event payload) gain:
- `category` — the routing decision.
- `trace` — `{classifier: {method, confidence, fallback_used}, category, seed_chunk_ids, expansion_candidates, reranked_count}`. Surfaced behind a UI toggle (Slice 5h) so eval / debugging can see the routing path without parsing logs.

The SSE `sources` event widens from a single chunk list to a typed dict (`chunks` / `commits` / `prs` / `issues`) so the frontend renders per-type panels without branching on a polymorphic flat list. Each entity in the payload carries its own `permalink` (built from `repo.owner` + `repo.name` + per-type identifier + commit SHA for chunks) so the UI's "Open on GitHub" link works for any retrieved entity, not just the ones the model cited.

## Consequences

**Upside**

- Keyword classifier costs nothing — latency-neutral default for the most common queries.
- LLM classifier is a one-env-var flip away when accuracy matters more than latency (Slice 7 ablation).
- Multi-hop only fires for `historical_why`; the other 4/5 categories pay no extra cost. Bounded depth + breadth keep rerank cheap.
- Single SQL recursive CTE for traversal — no N+1 round-trips. Indexes already designed for both outbound and inbound walks.
- `historical_why` answers can trace cross-domain chains (issue → PR → commit → code) the way humans would, with mechanically-verifiable typed citations end-to-end. Live verification on asyncer produced exactly this: a chunk + three commits cited correctly with SHA-pinned permalinks.
- Debug trace is in the response payload, not behind a logger — eval scripts get it for free.

**Costs / limitations**

- 7B-class local LLMs partially follow the typed-token format. Live testing showed Qwen 2.5 Coder 7B emits `[chunk:cN]` correctly (familiar Slice 4 pattern) but slips on the new types — writes "commit [m2]" with the namespace word as prose rather than `[commit:m2]`. Captured as a model-fidelity signal for Slice 7 eval, not a code fix. Gemini Flash class models handle the format cleanly.
- Out-of-scope detection in the keyword classifier is intentionally weak (it returns `lookup` with low confidence, doesn't actively flag); real out-of-scope detection is the LLM classifier's job or the post-retrieval confidence check's. The classifier-level `out_of_scope` is reachable via `classification_override` for explicit testing.
- The 2-hop limit means a `chunk → commit → pr → issue` chain that needs the issue in context only sees issues that surface via flat retrieval (or `closed_by` inverse). Conservative by design — raise after Slice 7 eval if under-retrieval shows.
- Rerank drops embedding-less candidates silently. A stub commit (`message=""`) inserted by Slice 5c's blame fallback won't be embedded by Slice 5d's orchestrator, so it can be in expansion but never in the final context. Acceptable — those commits carry no semantic signal anyway.

**Rejected alternatives**

- *LLM classifier as default* — adds 200–500ms to every query; latency cost outweighs accuracy gain for the common shallow categories. Slice 7 eval will quantify the actual accuracy delta.
- *No classifier — always run multi-hop* — wastes the cost on simple "where is X?" queries and balloons LLM context for no benefit.
- *Cross-encoder reranker* — better quality than pure cosine, but adds another model (latency + memory). Deferred to Slice 6.
- *Flat union over typed entities* — running flat retrieval against the polymorphic `entity_embedding` table for all types would mix code chunks and PR bodies in the top-k. Reranks badly because code embeddings and natural-language embeddings sit in different parts of the bge-small space. Stage-1 stays chunk-only.

**Re-evaluation triggers**

- If Slice 7 eval shows the keyword classifier mis-routing >5% of queries (especially `lookup ↔ historical_why`), flip the default to LLM (or invest in better keyword patterns).
- If 7B-class local models can't be coaxed into the typed-token format with prompt tweaks, drop back to a single token namespace (`[src:c1]`/`[src:m1]`/etc.) and infer entity_type from the letter prefix.
- If multi-hop's `reranked_count` is consistently 0 because non-chunk entities aren't embedded (a user runs `/ingest-history` but forgets to re-run `/embed`), surface a UI warning at query time.
- If the 2-hop depth limit hurts answer quality on multi-step chains in the eval set, raise to 3.
