# ADR 0013: Polymorphic widening of `chunk_embedding` to `entity_embedding`

**Status**: Accepted
**Date**: 2026-06-21

## Context

Slice 3 (ADR 0009) created a `chunk_embedding` table FK-bound to `code_chunk`. One row per chunk, one HNSW index, one dimension guard on startup. Simple and worked.

Slice 5 needs to embed commit messages, PR titles+bodies, and issue titles+bodies alongside chunks — the multi-hop reranker (ADR 0012) scores expansion candidates against the query vector, and that requires every candidate type to live in the same vector namespace so a single SQL query can rank them.

Two viable shapes:

1. **Polymorphic widening** — rename `chunk_embedding` to `entity_embedding`, replace the `chunk_id` FK with `(entity_type, entity_id)`, store all four entity types in one table.
2. **Parallel tables** — keep `chunk_embedding` untouched, add a separate `text_embedding` for non-chunk entities, UNION at query time.

Picked #1. This ADR captures why and how the migration was structured.

## Decision

### Single polymorphic table

```sql
entity_embedding (
  id PK,
  repo_id FK CASCADE,
  entity_type varchar(16),   -- 'chunk' | 'commit' | 'pr' | 'issue'
  entity_id   int,           -- PK in the corresponding table; NO FK (target varies)
  embedding   vector(384),
  model_name  varchar(64),
  dimension   int,
  created_at  timestamptz,
  INDEX (repo_id, model_name),
  INDEX (repo_id, entity_type, entity_id)
)
-- + HNSW vector index on `embedding`, built after bulk insert (Slice 3 pattern).
```

Mirrors `entity_edge`'s polymorphic shape (ADR 0011) — same tradeoff: lose FK enforcement, gain a uniform query surface.

### Migration is a rename + column add, not a new table

`c8d2f5a31e90` (the same migration that introduces the Slice 5 history schema) does:

```sql
DROP INDEX IF EXISTS chunk_embedding_hnsw_cos;
DROP INDEX  ix_chunk_embedding_repo_model;
DROP INDEX  ix_chunk_embedding_repo_id;
DROP INDEX  ix_chunk_embedding_chunk_id;
ALTER TABLE chunk_embedding DROP CONSTRAINT chunk_embedding_chunk_id_fkey;
ALTER TABLE chunk_embedding RENAME TO entity_embedding;
ALTER TABLE entity_embedding RENAME COLUMN chunk_id TO entity_id;
ALTER TABLE entity_embedding ADD COLUMN entity_type varchar(16)
  DEFAULT 'chunk' NOT NULL;     -- existing rows backfill to 'chunk'
CREATE INDEX ix_entity_embedding_repo_id          ON entity_embedding (repo_id);
CREATE INDEX ix_entity_embedding_repo_model       ON entity_embedding (repo_id, model_name);
CREATE INDEX ix_entity_embedding_repo_type_id     ON entity_embedding (repo_id, entity_type, entity_id);
```

Postgres `RENAME TABLE` keeps row data + the `vector(384)` type intact. A follow-up `d3e1a47b2104` migration renames the leftover identifiers (PK sequence, PK index, FK constraint) so `\d` output isn't half-renamed — purely cosmetic but worth doing for portfolio clarity.

The Slice 3 HNSW index `chunk_embedding_hnsw_cos` is dropped and recreated as `entity_embedding_hnsw_cos` by the embed orchestrator on next run.

### Per-type embedding orchestrator (Slice 5d)

`app/embeddings/orchestrator.py::embed_repo` was rewritten as a four-stage sequential pipeline (chunks → commits → PRs → issues). Each stage:

- Fetches `(id, text)` pairs from its type's table (text composition per type below).
- Deletes the repo's prior embeddings *for its `entity_type` only* via `DELETE … WHERE entity_type = …`. Other types' rows survive — re-running `/embed` after fresh history ingestion catches up the new types without re-encoding chunks (and vice versa).
- Batch-encodes (32-item batches) via the configured `Embedder`.
- Inserts new `EntityEmbedding` rows with `entity_type` set.

Text composition:
- **chunk** — `code_chunk.content` (unchanged from Slice 3)
- **commit** — `commit.message` (subject + body)
- **pr** — `title + "\n\n" + body`
- **issue** — `title + "\n\n" + body`

Stage with no rows (e.g. history not yet ingested) is a no-op. Stage whose batches all fail to encode doesn't fail the overall job — `failed` only fires when *every* stage produced zero embeddings and at least one had work to do.

### Status endpoint widens; rerank stays single-query

`GET /repos/{repo_id}/embedding-status` returns `chunks_total / chunks_embedded` plus per-type counts (`commits_total / commits_embedded` etc.) via one `GROUP BY entity_type` query. Old fields kept (`chunks_total / chunks_embedded`) so Slice 4 callers see the same shape.

The multi-hop reranker (ADR 0012) does ONE cosine query against `entity_embedding` filtered to the expansion candidate set, with `tuple_(entity_type, entity_id).in_(…)`. No UNION across tables; no per-type code path in retrieval.

## Consequences

**Upside**

- Single retrieval query covers all entity types; the multi-hop reranker is six lines instead of four UNION'd subqueries.
- Single HNSW index — one set of vector-ops parameters to tune, one dimension guard at startup.
- New entity type = new stage in the embed orchestrator + new fetch function. No schema migration per type.
- Per-type idempotent re-embed lets `/ingest-history` → `/embed` → ask query work without requiring a chunks re-encode.
- Existing 132 asyncer chunk embeddings were backfilled to `entity_type='chunk'` by the `ADD COLUMN … DEFAULT 'chunk'` clause — no data loss across the rename.

**Costs / limitations**

- Lost FK on `entity_id` — same polymorphism tradeoff as `entity_edge`. Mitigation: the embed orchestrator is the only writer, runs per-`repo_id`, and re-embeds delete-and-replace per type so a stale `entity_id` would be flushed on the next run.
- A stub commit (empty message) inserted by Slice 5c's blame fallback has nothing to embed and gets dropped silently. It exists in the graph (for the `introduced_by` edge target) but never in the multi-hop final context.
- The 384-dim space mixes code (bge-small over `code_chunk.content`) and natural language (bge-small over commit/PR/issue text). Different distributions; reranking quality is a Slice 7 eval-time concern. Acceptable for v1.
- Slice 4's `chunk_embedding` field references in tests + Slice 3 docs are now outdated; updated in the same migration window.

**Rejected alternatives**

- *Parallel `text_embedding` table* — preserves Slice 3 untouched but doubles the query surface (every retrieval becomes a UNION ALL) and creates two HNSW indexes to maintain. Cleanliness loss outweighs the migration risk we'd avoid.
- *Different embedder per type* (code-specialized for chunks, sentence-tuned for commits/PRs/issues) — better domain-fit per type but defeats the single-rerank-query property. Saved for ablation.
- *Embed only chunks; pull non-chunk text inline at query time* — sidesteps the migration, but the reranker has nothing to score commits against without an embedding. Defeats the multi-hop pipeline's purpose.

**Re-evaluation triggers**

- If Slice 7 ablation shows a code-specialized embedder (voyage-code-2) significantly out-performs bge-small on chunks, consider splitting back to per-type tables so each type can have its own embedder.
- If the polymorphic `(entity_type, entity_id)` index becomes hot and pgvector can't keep up with mixed-type filtering on huge repos, partition `entity_embedding` by `entity_type`.
- If we ever embed entity types that genuinely shouldn't be ranked against each other (e.g. file-level summaries vs. function chunks), revisit the single-namespace assumption.
