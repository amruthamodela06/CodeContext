# ADR 0014: Hybrid retrieval (BM25 + vector, RRF-fused) + cross-encoder reranker

**Status**: Accepted
**Date**: 2026-07-08

## Context

Slice 3 (ADR 0009) put pgvector cosine search behind the Slice 4 answer pipeline. That's a fine baseline, but a portfolio project called "conversational code intelligence" that only ever fires one retrieval strategy makes a weak Slice 7 evaluation story. To claim "hybrid improves recall@5 by X points over vector-only", the app needs both strategies wired behind an A/B switch, plus the fusion + rerank machinery to fairly compare four modes (vector / BM25 / hybrid / hybrid + rerank).

The switch has to be trivial to flip — one env var, no code path branch — so the eval harness can loop over modes in Slice 7. The retrievers also have to be uniform enough that Slice 5's multi-hop stage 1 can call the configured retriever without knowing which mode is active.

## Decision

### Postgres full-text search — one generated tsvector per entity

Slice 6a added a `fts_tsv tsvector` column to each of the four retrievable tables (`code_chunk` / `commit` / `pull_request` / `issue`), backed by a GIN index. tsvector is a `GENERATED ALWAYS AS ... STORED` column so it stays in lockstep with the source text at the schema level — no application code to keep two sides synced.

Weighted, per-type:

| Table | Weight A (top) | Weight B (mid) | Weight D (fallback) |
|---|---|---|---|
| `code_chunk` | `fts_name` (symbol + camelCase / snake_case splits) | `fts_doc` (extracted docstring) | `fts_body` (raw code) |
| `commit` | `split_part(message, E'\n', 1)` (subject) | full `message` | — |
| `pull_request` | `title` | `body` | — |
| `issue` | `title` | `body` | — |

Chunks are the tricky one — code isn't English. The `english` FTS tokenizer would treat `getUserByEmail` as one lexeme `getuserbyemail` and never match a query like `user email lookup`. So chunks store three app-managed TEXT intermediates (`fts_name` / `fts_doc` / `fts_body`) populated by Slice 6b's `compute_chunk_fts` at ingest time; the tsvector is generated from those. Splitting is done in Python once, at index time.

The specific splitter (`split_identifier`, Slice 6b):
- Preserves the original identifier verbatim so exact-name search still hits.
- Adds camelCase / snake_case / hyphen splits as additional tokens.
- Case is preserved (english tokenizer lowercases at index time; either form collapses to the same lexeme).

Ranking uses `ts_rank_cd(fts_tsv, plainto_tsquery('english', :q), 32)`. The `32` normalization bit divides by `1 + log(document length)` — critical because chunk bodies are 300 LOC and commit subjects are 50 chars; without normalization, length would dominate.

**Rejected alternative: naive FTS baseline.** Skipping identifier splitting would give a "purer" BM25 baseline for hybrid to beat — but the point of Slice 7 isn't "which BM25 variant is best", it's "does hybrid help at all". Artificially weakening BM25 makes hybrid's win over vector-only misleadingly larger. Code-aware split is the honest baseline.

### Retriever protocol

```python
class Retriever(Protocol):
    name: str
    async def retrieve(
        self,
        session: AsyncSession,
        repo_id: int,
        query: str,
        top_k: int,
        filters: RetrievalFilters | None = None,
    ) -> list[RetrievalResult]: ...
```

Retrievers return **pointers** (`entity_type` + `entity_id` + `score` + `score_breakdown`), not hydrated rows. Hydration to `CitedChunk` / `CitedCommit` / `CitedPR` / `CitedIssue` stays in Slice 5's `retrieve_entities` orchestrator, which already knows how to walk per-type tables and render typed citations. Clean separation: retrievers rank, orchestrator hydrates.

`RetrievalFilters.entity_types` narrows to a subset. Slice 6h wires `entity_types={"chunk"}` for stage-1 retrieval because history entities enter the context via multi-hop expansion (Slice 5f), not flat retrieval. Filters propagate through hybrid + rerank layers untouched.

`score_breakdown` is a debug surface, not a public contract — keys vary by retriever (`vector_score`, `distance`, `bm25_score`, `vector_rank`, `bm25_rank`, `rrf_score`, `rerank_score`). Slice 6i pipes it through to the frontend debug panel; downstream code MUST NOT branch on specific keys.

Third instance of the provider pattern after `Embedder` (Slice 3) and `LLMProvider` (Slice 4), so the shape was already settled by the time this landed.

### Four modes, one env var

`RETRIEVAL_MODE` (config field on `Settings`) selects:

| Mode | Concrete | What runs |
|---|---|---|
| `vector` | `VectorRetriever` | pgvector cosine over `entity_embedding` (Slice 3 baseline kept for ablation). |
| `bm25` | `BM25Retriever` | Postgres FTS UNION ALL over four `fts_tsv` columns. |
| `hybrid` | `HybridRetriever` | Vector + BM25 in parallel via `asyncio.gather`, RRF-fused (default). |
| `hybrid_rerank` | `RerankedRetriever(HybridRetriever)` | Hybrid + cross-encoder rerank on top-20. Opt-in. |

`get_retriever()` is `@cache`-wrapped so downstream calls share the same instance across a process — the cross-encoder model loads once, not per request. Unknown values log a warning and fall back to `hybrid` rather than crashing — Slice 7 eval iterates through modes and a typo shouldn't take the app down.

### RRF — the standard formula, in Python

```
score(d) = sum over rankers r of  1 / (k + rank_r(d))
```

Rank is 1-indexed. `k` is a smoothing constant; literature default 60, exposed as `RRF_K` env for Slice 7 ablation. Missing rankers contribute 0 explicitly — an entity that appears in only one ranker's top-N still fuses, just with that one ranker's contribution.

RRF operates on **ranks alone**, not raw scores. Vector produces cosine similarities in `[0, 1]`; BM25 produces `ts_rank_cd` values that can be > 2. Raw sum-fusion would let BM25 dominate every query with any lexical match. RRF is invariant to the underlying score scale — that's the whole reason to use it here.

Implementation lives at `app/retrieval/rrf.py`; pure Python, no DB. Combining result sets at the application layer is cleaner than doing it in SQL (which would need a full-outer-join between two subqueries per invocation, with per-row rank window functions on each side).

### `HybridRetriever` — parallel + fused

```python
HybridRetriever(vector=None, bm25=None, *, candidate_n=50, rrf_k=60)
```

- Runs `vector.retrieve(top_k=candidate_n)` and `bm25.retrieve(top_k=candidate_n)` in `asyncio.gather` — wall clock = max(vector, BM25), not sum.
- Fuses the union via RRF, truncates to caller's `top_k`.
- `vector` / `bm25` are DI'd (default constructor produces fresh instances); the factory can pass tuned singletons.
- `candidate_n` (`RETRIEVAL_CANDIDATE_N` env) governs how many candidates each retriever contributes before fusion; higher means broader union at the cost of a longer BM25 UNION query.

Filters propagate to both underlying retrievers — an `entity_types={"chunk"}` filter on the outer call reaches the inner vector + BM25 SELECTs so the SQL narrows at the source, not after fusion.

### Cross-encoder rerank — bge-reranker-base via sentence-transformers

```python
RerankedRetriever(inner, *, input_n=20, model_name="BAAI/bge-reranker-base", model=None)
```

The inner (typically `HybridRetriever`) pulls `input_n=20` candidates. The cross-encoder scores each `(query, passage)` pair independently — reading both together through its trained relevance head — and returns the top-`top_k` by that score.

Cost is real: **~50 ms per pair on CPU with bge-reranker-base, so ~1–2 s for the default 20 pairs**. That's why the rerank mode is opt-in (`hybrid_rerank`); the default `hybrid` doesn't pay it.

Library choice: `sentence-transformers`' `CrossEncoder` class, already in the dep tree (Slice 3's `SentenceTransformersEmbedder` uses the same package). Same underlying weights as `FlagEmbedding` (BAAI's own wrapper) within float noise, no new top-level dep.

Model cache: standard HuggingFace location (`~/.cache/huggingface/`) — matches Slice 3's embedder cache, so users who ran `/embed` once have a warm HF cache. Explicit `HF_HOME=./models` would look tidier but re-downloads on every CI / cold deploy, so we inherit HF's convention.

Content per type follows a "content only, no metadata" rule:

| Type | Passage text |
|---|---|
| chunk | raw `content` (function/class body) |
| commit | full `message` (subject + body) |
| pr | `title + "\n\n" + body` |
| issue | `title + "\n\n" + body` |

The cross-encoder was trained on generic `(query, passage)` pairs from MS MARCO and similar corpora. Prefixing headers like `# src/auth.py::validate()\n\n` puts the model in a distribution it wasn't trained on and empirically hurts more than it helps.

**Known imperfection**: bge-reranker-base has a 512-token context. Long chunks (300+ LOC) get head-truncated by the tokenizer. Acceptable — chunks are function-scoped and the signature + docstring are usually the discriminator. Slice 7 measures whether sliding-window rescoring is worth the 3–5× cost.

**Lazy load + DI seam**: the real model isn't touched until the first `retrieve()` call. Tests inject a lightweight stub via the `model` kwarg; the `_CrossEncoderProtocol` type documents the minimal surface (`predict(pairs) -> list[float]`). `predict` is sync CPU-bound so it runs under `asyncio.to_thread` to keep the event loop free.

### `score_breakdown` accumulates through the pipeline

Each retriever adds its own keys and forwards the inner's breakdown:

- `VectorRetriever` → `{vector_score, distance}`
- `BM25Retriever` → `{bm25_score}`
- `HybridRetriever` → `{vector_rank, bm25_rank, rrf_score}` (via RRF)
- `RerankedRetriever` → `{...inner keys..., rerank_score}`

So a top result from `hybrid_rerank` carries `vector_rank + bm25_rank + rrf_score + rerank_score` all together. Slice 6i pipes it through the SSE `sources` event to the frontend debug panel.

### Stage timings on the trace

`retrieve_entities` (Slice 5 orchestrator) stamps `trace["stage_timings_ms"]` at each boundary — `retrieve` (stage 1), `expand` (multi-hop CTE), `multihop_rerank` (embedding rerank of expansion set), `hydrate` (Cited* materialization). Only stages that actually ran show up. Rounded to 0.1 ms via `time.perf_counter`.

Purpose: `/query` debug panel + Slice 7 eval scripts. Real p50/p95 across the four modes will be reported once Slice 7 measures them.

## Consequences

**Upside**

- One env var flip cycles through four retrieval strategies. Slice 7's ablation table writes itself.
- All modes share one hydration path and one prompt/citation pipeline — the diff between modes is exactly the ranking, not any downstream behavior.
- BM25 catches exact-symbol / rare-token queries that dense embeddings miss (verified live on asyncer: `syncify` matches lexically before it matches semantically).
- Vector catches paraphrased / conceptual queries that lexical search misses ("run async from sync" → chunks that don't literally contain those words).
- RRF is scale-invariant across retrievers — no calibration step needed to weight cosine against ts_rank_cd.
- Cross-encoder rerank is a one-instance-var flip when higher quality is worth the latency; the default doesn't pay for it.
- `score_breakdown` is verbose enough that a bad ranking can be diagnosed in the browser (see: which retriever contributed which rank).
- Zero API cost added — all four modes run local (Postgres FTS, pgvector, local model).

**Costs / limitations**

- Weighted ts_rank_cd on chunks is heuristic — no principled reason for A/B/D over A/B/C. Slice 7 eval will surface whether the weighting is well-tuned.
- Single embedder covers code + English (commit / PR / issue text). A code-specialized embedder + a text-specialized embedder would rank each type better, at the cost of two HNSW indexes to maintain. Deferred.
- The `english` FTS config is applied to code identifier splits — technically wrong (identifiers aren't English), but stemming rarely fires on split tokens like `get` or `user`, and switching to `simple` would lose stopword stripping on genuine English text (docstrings, commit messages, issue bodies).
- bge-reranker-base's 512-token limit head-truncates long chunks.
- No query rewriting / expansion (synonym expansion, LLM query augmentation). Explicitly out of scope for Slice 6; a future slice may add it.
- `RETRIEVAL_MODE` typos silently fall back to `hybrid`. Warns in the log. Chose this over hard-fail because eval harnesses set env dynamically and a hard fail cascades badly across a sweep.

**Rejected alternatives**

- *Weighted linear fusion* (`score = alpha * vector + (1 - alpha) * bm25`) — requires normalizing the two score scales, and even after normalization, tuning `alpha` is per-corpus. RRF sidesteps both by operating on ranks.
- *Fully-generated tsvector for chunks* (no Python-managed intermediates) — would require doing camelCase/snake_case splitting inside a SQL function via `regexp_replace` chains. Ugly, slow, and inflexible. Python once at index time is cleaner.
- *Cross-encoder as default* — the ~1–2 s latency cost per query would make interactive use painful, and the answer quality gain is a Slice 7 measurement, not an assumption.
- *Reranker with metadata prefixes* (`# file.py::func()\n\n<content>`) — tested against content-only in the field; adds distribution shift with no measurable win. Slice 7 revisits if the eval surfaces a case where headers help.
- *`FlagEmbedding` dep for the reranker* — same weights, adds a top-level dep for what `sentence-transformers.CrossEncoder` already provides. No.
- *Explicit `HF_HOME=./models`* — makes the model cache in-project but breaks cache sharing with Slice 3's embedder, forces re-downloads on cold deploys. HF default wins.
- *Per-type embedder swaps* (code-specialized for chunks, sentence-tuned for history) — better fit per type but breaks the single-query rerank story. Saved for a future eval-driven decision.

**Re-evaluation triggers**

- If Slice 7 eval shows `hybrid` beats `vector` by less than ~5% recall@5, revisit whether the fusion complexity is earning its keep.
- If `hybrid_rerank` wins by less than ~3% over `hybrid` in the eval, drop it from the default demo path (still keep for ablation).
- If the code-aware identifier splitter hurts English-text queries (docstring / message body matches), consider gating splits by fts_name only and leaving the tokenizer alone elsewhere.
- If per-query rerank latency crosses 3 s on the asyncer fixture in the perf test, either shrink `RERANKER_INPUT_N` or investigate a smaller reranker (bge-reranker-v2-m3-base).
- If a code-specialized embedder (`voyage-code-2`, `nomic-embed-code`) beats bge-small on chunks by a meaningful margin in Slice 7, consider splitting `entity_embedding` back to per-type tables so each type can carry its own embedder.
