# ADR 0009: Embeddings + vector storage + search

**Status**: Accepted
**Date**: 2026-05-27

## Context

Slice 3 turns the `code_chunk` rows from Slice 2 into retrievable vectors: generate embeddings with a local CPU model, store them in pgvector, expose a naive cosine `POST /search`. The `Embedder` interface locked here outlives this slice — hybrid retrieval (Slice 4) and the LLM answer slice build on it. CPU-only, no API embeddings in the default path, no GPU (ADR 0007 + PRD §9 dev-env note).

This ADR refines the embedding default named in ADR 0007 (which fixed the *provider family* as `bge` and named the `EMBEDDING_PROVIDER` env, but not the exact variant).

## Decisions

### Default model: `bge-small-en-v1.5` (384-dim)

ADR 0007 said "bge family, bge-base default". Slice 3 flips the default variant to **bge-small**:

- ~2-3× faster on CPU than bge-base (~50-80 vs ~20-30 chunks/sec). A 50k-chunk repo embeds in ~10-17 min instead of ~25-40. For a portfolio project where ingestion has to be demo-able, that wait difference is material.
- ~1-3% MTEB quality gap — empirically small for code retrieval, where chunk specificity dominates.
- 384-dim vs 768-dim: half the storage, faster index ops.
- The `Embedder` factory makes a later flip cheap; bge-base remains a registered variant for the eval-slice ablation (PRD §8 lists "bge-small vs bge-base" explicitly).

PRD §9.1, CLAUDE.md locked-choices, and ADR 0007 are updated to name bge-small as default with bge-base as the ablation comparison.

### Library: `sentence-transformers`

Wraps the HF model. Pulls `torch` (CPU). Verified before adoption: torch 2.12.0 ships a `cp314-cp314-win_amd64` wheel, so it installs on our Python 3.14 / Windows dev box. Model is lazy-loaded on first `embed_*` call; cached under a project-local `./.hf-cache` volume (see "Model cache").

### `Embedder` interface

```python
class Embedder(ABC):
    @property
    def name(self) -> str: ...        # "bge-small-en-v1.5"
    @property
    def dimension(self) -> int: ...   # 384
    def embed_one(self, text: str) -> list[float]: ...
    def embed_batch(self, texts: list[str]) -> list[list[float]]: ...
```

Concrete: `SentenceTransformersEmbedder(model_name)` for `bge-small` (default) and `bge-base`; `FakeEmbedder` for tests (deterministic, with a semantic-similarity invariant). Stubs raise `NotImplementedError`: `OpenAIEmbedder`, `VoyageEmbedder`, `OllamaEmbedder`.

`EMBEDDING_PROVIDER` carries the full identity (`bge-small`, `bge-base`, `mock`, future `openai:…`). One env var, no secondary knob.

### Storage: separate `chunk_embedding` table

```
id            int PK
chunk_id      int FK -> code_chunk.id  ON DELETE CASCADE
repo_id       int FK -> repo.id        ON DELETE CASCADE   (denormalized: repo-scoped search without a join)
embedding     vector(384)
model_name    varchar(64)
dimension     int
created_at    timestamptz
```

Index `(repo_id, model_name)` for filtering. **No unique constraint** on `(chunk_id, model_name)`; re-embed deletes the repo's existing rows then re-inserts (same upsert pattern as `chunk_repo`). The `vector(384)` dimension is fixed at column creation — changing to a different-dim model means a migration + re-embed, documented as a known v1 cost. A startup assertion compares one stored row's `dimension` to the active embedder's `dimension` and refuses to boot on mismatch (prevents silent corruption).

### Index: HNSW, built *after* bulk insert

`USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64)`. Better recall + faster queries than IVFFlat for our "embed once, search many" pattern.

**Build ordering**: the migration creates the table *without* the index. The embed orchestrator runs `CREATE INDEX IF NOT EXISTS` after the bulk insert completes. Building on an empty table then inserting 50k rows multiplies per-row maintenance cost ~10×; building once on populated data avoids that cliff. `IF NOT EXISTS` (rather than DROP+CREATE) means the first repo's embed builds it on populated data and later repos insert into the existing index incrementally — DROP+CREATE would rebuild the whole global index on every repo embed.

### pgvector extension + asyncpg type registration

- The `vector` extension is **not** enabled in our databases by default. The migration runs `CREATE EXTENSION IF NOT EXISTS vector` first. `tests/conftest.py` runs the same before `Base.metadata.create_all` (tests skip Alembic).
- **We do NOT register the asyncpg binary codec.** SQLAlchemy's pgvector `Vector` type serializes `list[float]` to pgvector's text format (`'[...]'`) and Postgres parses it on both insert and `<=>` queries. Registering `pgvector.asyncpg.register_vector` on top of the SQLAlchemy type causes a double-encode conflict — the binary codec's `np.asarray(value)` receives an already-stringified value and raises `could not convert string to float`. The asyncpg codec is only for *raw* asyncpg usage, not the SQLAlchemy `Vector` type. (Verified empirically during Slice 3; the Plan agent's "register per connection" advice was for the raw-asyncpg path.)

### Pipeline: FastAPI BackgroundTasks (no job queue yet)

`POST /repos/{repo_id}/embed` returns 202 immediately, sets `Repo.embedding_status='in_progress'`, and spawns a background task. The task uses a **fresh AsyncSession** (the request session is closed by the time it runs), embeds in batches of 32, updates `Repo.embedding_progress` per batch, builds the HNSW index at the end, sets `status='done'`. Per-chunk failures log + skip. `GET /repos/{repo_id}/embedding-status` is the poll endpoint.

**Orphan recovery**: a `lifespan` startup hook resets any `embedding_status='in_progress'` repo to `'failed'` — a uvicorn restart mid-embed surfaces as failed rather than stuck-forever. Cheaper than migrating to RQ/arq this slice; that migration is the documented escalation if single-process turns out insufficient.

**Warm-up**: the same lifespan hook calls `embed_one("warmup")` (skipped when `EMBEDDING_PROVIDER=mock`) so the first real `/search` doesn't pay model-load latency.

### Search

`POST /search {repo_id, query, top_k}` → embed query → `ORDER BY embedding <=> :q LIMIT k` → return chunks with `similarity = 1 - cosine_distance`, file path, line range, content.

### Model cache

Local dev (running `make backend-dev` outside a container) uses the host
HuggingFace cache (`~/.cache/huggingface` on Linux/macOS, `%USERPROFILE%\.cache\huggingface` on Windows) — the model downloads once on first embed and persists there. The backend is **not** containerized yet (docker-compose runs only Postgres), so there is nothing to mount a model-cache volume into right now.

When the backend is containerized (deployment slice), mount a persistent volume at the container's HF cache path so the ~130 MB model isn't re-downloaded on every container restart. `./.hf-cache/` is pre-added to `.gitignore` for that future project-local mount. The model is never baked into the image (keeps it lean); it lazy-loads on first use.

## Consequences

**Upside**: zero-cost CPU-local default; ablation falls out of the `Embedder` factory; offline-capable; no API key for the default path. HNSW gives sub-second search at our scale.

**Costs**: `torch` is a heavy dep (~200 MB+ installed). bge-small's 384-dim is fixed in the column — a model change is a migration. CPU embedding of large repos is genuinely slow (minutes), surfaced honestly via the status field. BackgroundTasks loses the job on process restart (mitigated by orphan recovery, not eliminated).

**Re-evaluation triggers**: if CPU throughput can't index 50k chunks in ~15 min, reconsider an API embedder for the default; if the single-process job model causes real pain (frequent restarts, concurrent embeds), migrate to arq; if eval shows bge-small materially underperforms bge-base on the flagship, flip the default (cheap via the factory + a re-embed).
