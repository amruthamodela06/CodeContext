# PRD: CodeContext — Conversational Code Intelligence for GitHub Repos

**Author:** Amrutha
**Status:** Draft v2 (incorporates technical review feedback)
**Last updated:** May 2026
**Scope:** This document covers **v1 only**. v1.1, v2, and beyond are tracked separately in `docs/roadmap.md`.

---

## 1. Summary

CodeContext is a web application that lets developers ask natural-language questions about any public GitHub repository and get accurate, cited answers grounded in the repo's code, commit history, pull requests, and issues. Unlike code search (which finds *where* something is) or generic chatbots (which hallucinate), CodeContext retrieves from real repository data and cites its sources with verifiable references, enabling questions that traditional tools can't answer — particularly *why* questions about historical decisions.

The v1 goal is a deployed, demoable web product with rigorous evaluation on at least one flagship repo, supporting on-demand ingestion of any public repo within scope limits.

---

## 2. Problem

Engineers joining a new codebase, contributing to an open-source project, or debugging an unfamiliar system spend significant time reconstructing context that already exists somewhere in the repo's history. Specifically:

- **Code search tools** (grep, GitHub search, IDE search) find symbols but not intent.
- **Generic AI chatbots** hallucinate code that doesn't exist or describe codebases they don't actually know.
- **Documentation** is incomplete, outdated, or doesn't cover historical "why" questions.
- **Asking a teammate** requires their availability and assumes they remember.

The "why" information exists — it's in commit messages, PR descriptions, issue threads, and code review comments — but it's not searchable in any practically useful way.

---

## 3. Goals and non-goals

**Goals**

- Allow users to ask natural-language questions about any public GitHub repo and receive answers grounded in actual repo data
- Cite all claims with mechanically-verified references to specific files, lines, commits, PRs, or issues
- Support four question categories: factual lookup, architectural understanding, historical/intent ("why"), and impact analysis ("what depends on this")
- Deliver demonstrable retrieval quality on at least one flagship repo with quantitative evaluation
- Ship as a publicly deployed web app with a live URL

**Non-goals (explicitly out of scope for v1)**

- Private repo support (avoids OAuth-scope complexity, privacy/legal surface, and storage liability)
- Code generation, editing, or PR drafting (this is a retrieval product, not a Copilot competitor)
- Real-time index updates (manual refresh button is sufficient)
- Multi-repo / cross-repo queries (single repo per session)
- IDE integration (web only)
- Mobile-optimized UI (desktop-first; mobile is best-effort)
- Account-bound persistent conversation history (browser-local for v1)

---

## 4. Target users

**Primary**: Software engineers exploring an unfamiliar open-source codebase — to evaluate it, contribute to it, or learn from it.

**Secondary**: Engineers researching how a specific problem has been solved in well-known OSS projects (e.g., "how does FastAPI handle dependency injection?").

**Tertiary**: Engineering students and bootcamp graduates studying production codebases for learning.

**Not targeted in v1**: Enterprise teams analyzing private codebases (requires a different product surface, auth model, and trust posture).

---

## 5. User stories

- As a developer evaluating a new OSS library, I want to ask "how does this library handle X?" and get a grounded answer with citations, so I can decide whether to adopt it without reading the whole codebase.
- As a new contributor to a project, I want to ask "why was this designed this way?" and see the historical PRs and discussions, so I understand the rationale before suggesting changes.
- As an engineer debugging a third-party dependency, I want to ask "what changed in version X.Y?" and see specific commits and PRs, so I can isolate the source of a regression.
- As a learner, I want to ask "show me an example of how authentication is implemented" and get real code from real projects, so I can study patterns in production code.

---

## 6. Functional requirements

### 6.1 Repo ingestion

- Users can ingest any public GitHub repo by URL or by selecting from a list of pre-indexed popular repos
- Ingestion limits: max 200,000 lines of code **and** max 5,000 files. Both caps apply independently; whichever is hit first triggers the limit. A repo with 4,000 files totaling 250k LOC is rejected by the LOC cap; a repo with 6,000 files totaling 100k LOC is rejected by the file cap. Users are shown which cap was exceeded with a clear error and a suggestion to specify a subdirectory.
- Ingestion pipeline indexes: source code (AST-chunked), commit history (last 12 months by default, full history opt-in), merged PRs, closed issues, and the README/docs directory
- Progressive availability: file structure browsable within 10s, symbol lookups within 30s, semantic search within 2min, full historical queries within 5min
- Progress is shown to the user with stage-level indicators

### 6.2 Querying

- Single text input for natural-language questions
- Streaming token output as the LLM generates
- Every factual claim in the answer is wrapped in a citation that mechanically resolves to a specific source — see §9.4 for the citation accuracy mechanism
- Citations expand inline to show the relevant snippet without leaving the page; "open on GitHub" link uses the commit SHA pinned at ingestion time (not `main`), to handle code that has shifted since indexing
- A collapsible "sources retrieved" panel shows which documents the retrieval surfaced, with relevance scores
- Suggested starter questions are shown for each repo (auto-generated from repo structure)
- **Out-of-scope question handling**: if retrieval returns no chunks above a relevance threshold (default cosine similarity < 0.35), the LLM is prompted to respond with "I don't see anything in this repo about that — this may be outside the indexed scope" rather than answering from general knowledge. Enforced via prompt and verified on a dedicated out-of-scope subset of the eval (see §8).
- **Low-confidence retrieval**: when top-result scores are below a soft threshold or there are large gaps between results, the UI displays a "low confidence" indicator and surfaces retrieved chunks more prominently, encouraging the user to verify directly.

### 6.3 Conversation

- Follow-up questions within a session use prior context via **query rewriting**: a lightweight LLM call rewrites the follow-up into a standalone query using the conversation history, and the rewritten query goes through the standard retrieval pipeline. This is preferred over concatenating prior context because retrieval against a focused query outperforms retrieval against a long conversation transcript. The original follow-up is preserved for display; the rewritten query is shown in the "sources" panel for debugging.
- Conversation history is preserved per repo per session (browser-local for v1; account-bound is deferred to v2)
- "Clear conversation" and "new chat" actions

### 6.4 Feedback

- Thumbs up / thumbs down on every answer
- Optional free-text feedback on thumbs-down
- Feedback is stored with the query, retrieved sources, generated answer, and resolved citations for later analysis

### 6.5 Repo management

- "Refresh repo" button to re-ingest (rate-limited to once per 24 hours per repo)
- User-ingested repos auto-expire from the index after 14 days of no queries; users can re-ingest on demand
- **Pre-indexed popular repos are exempt from expiration** and are refreshed on a 7-day rolling schedule via a scheduled job
- A "popular repos" page shows pre-indexed repos for instant access

### 6.6 Cost controls

- Anonymous users: 20 queries per day, can only query pre-indexed popular repos
- Signed-in users (GitHub OAuth, public repo scope only): 50 queries per day, can ingest any in-scope repo
- BYO-API-key users: unlimited queries, ingest unlimited repos
- All limits visible and explained in-app

---

## 7. Non-functional requirements

**Query latency** (broken out, since "latency" with streaming is ambiguous):
- Time-to-first-token (TTFT): p50 < 2s, p95 < 4s
- Total time-to-complete-answer: p50 < 5s, p95 < 10s (variable with answer length)
- Retrieval-only latency (excluding LLM generation): p50 < 800ms, p95 < 2s

**Ingestion latency**: 200k LOC repo fully indexed in < 5 minutes end-to-end.

**Retrieval quality targets** (reported separately per eval set):
- Hand-curated flagship eval: recall@5 ≥ 0.75 overall, ≥ 0.65 on the `historical_why` subset
- Auto-generated Tier 1 eval (across at least 2 repos): recall@5 ≥ 0.70
- The hand-curated set is the primary quality measure; the auto-generated set is the generalization check.

**Citation accuracy targets** (see §9.4 for definitions):
- ≥ 99% of returned citations parse to a valid retrieved chunk_id (mechanical accuracy)
- ≥ 90% of citations point to a chunk that human review judges relevant to the cited claim (semantic accuracy)

**Uptime**: best-effort 99% (this is a portfolio project, not an SLA product).

**Cost ceiling**: total infra + API cost per active user per month < $0.50 at expected v1 traffic, achievable via pre-indexing + caching + rate limits + local-default embeddings.

---

## 8. Evaluation plan

The evaluation framework is a first-class deliverable, not an afterthought. It's how we know the system works and how we make engineering decisions.

**Flagship-repo eval**: 60–80 hand-curated questions on one repo (FastAPI proposed — see §8.1 for rationale). Mix of: factual lookup (~25%), architectural (~25%), historical/intent (~25%), impact analysis (~15%), and **out-of-scope (~10%)**. Each question has labeled ground-truth sources and an expected-answer rubric.

**Auto-generated eval (Tier 1)**: Pipeline that, given any new repo, generates ~50 evaluation questions automatically from: PR descriptions ("Why was X changed?"), closed issues linked to fix PRs ("How was Y fixed?"), and AST-derived symbol lookups ("Where is Z defined?"). Used to validate generalization to repos beyond the flagship.

**Metrics tracked per release**:

- Retrieval recall@5 and recall@10, reported overall and per question category
- Citation accuracy (mechanical + semantic, per §9.4)
- Answer quality (LLM-as-judge with documented rubric; hand-spot-checked on a 20-question sample)
- Out-of-scope refusal rate (% of out-of-scope questions correctly declined)
- TTFT and total latency, p50 and p95
- Cost per query (per provider)

**Ablation studies** to be reported in the v1 README. Each ablation reports the delta on recall@5 and citation accuracy:

- AST vs. naive chunking
- Vector-only vs. hybrid (BM25 + vector) retrieval
- With reranker vs. without
- Flat retrieval vs. multi-hop graph-augmented retrieval (on `historical_why` subset only)
- bge-small vs. bge-base embeddings
- Qwen 2.5 Coder 3B (local) vs. Gemini 2.0 Flash vs. GPT-4o-mini as LLM

### 8.1 Why FastAPI as the flagship eval repo

FastAPI was selected because:
- **Mid-sized** (~50k LOC) — within v1 ingestion limits without requiring partial-repo support
- **High-quality PR and issue discussions** — strong ground-truth material for the eval set
- **Active enough** to test "recent change" queries but **old enough** to have meaningful historical context
- **The maintainer's writing on design decisions** (in PR descriptions and docs) provides a natural answer key for `historical_why` questions

---

## 9. Technical architecture

### 9.1 Stack

**Backend**: Python 3.12 + FastAPI. `uv` for dependency management. Async ingestion workers via background tasks (RQ or arq if we outgrow background tasks).

**Storage**: Postgres with pgvector for both structured data (repos, files, commits, PRs, issues, the relationship graph from §9.5) and vector embeddings. Single-database simplicity for v1.

**Parsing**: tree-sitter for AST extraction and chunking. Languages supported in v1: Python, TypeScript/JavaScript, Go, Rust (in priority order).

**Embeddings**: `bge-base-en-v1.5` via `sentence-transformers`, running in-process on CPU. Abstracted behind an `Embedder` interface; swappable to OpenAI `text-embedding-3-small`, Voyage `voyage-code-2`, or Ollama `nomic-embed-text` for ablation.

**LLM**: Gemini 2.0 Flash via free tier as default. Abstracted behind an `LLMProvider` interface with implementations for `gemini` (default), `openai` (GPT-4o-mini), `anthropic` (Claude Haiku), and `ollama` (local Qwen 2.5 Coder 3B Instruct for offline development and ablation). Provider selected via `LLM_PROVIDER` env variable. All providers must support streaming.

**Retrieval**: Hybrid — Postgres FTS (BM25-equivalent via `ts_rank`) plus pgvector cosine similarity, fused with reciprocal rank fusion. Optional cross-encoder reranker (`bge-reranker-base`, CPU) on top-k results. See §9.5 for `historical_why` retrieval, which extends this pipeline with graph traversal.

**Frontend**: Next.js 14 (App Router), Tailwind, Monaco for code rendering in citations, server-sent events for streaming answers.

**Deployment**: Frontend on Vercel, backend on Railway or Fly.io, Postgres on Neon or Supabase. Custom domain.

**Observability**: Structured JSON logging, query/feedback events to Postgres, no third-party analytics in v1.

**Dev environment**: Primary dev machine is Windows 11 on AMD Ryzen 5 7535HS (integrated Radeon 660M, 16GB RAM). All local inference runs on CPU; no GPU acceleration assumed.

### 9.2 GitHub authentication for ingestion

- Public repo *content* is read via shallow `git clone` (no auth needed)
- PR and issue data is read via the GitHub GraphQL API using a service-account Personal Access Token, giving a 5,000 req/hour rate limit shared across all ingestion jobs
- For v1 this is sufficient given the pre-indexing strategy and per-repo rate limits
- v2 will migrate to a GitHub App for higher per-installation limits and the ability to support OAuth-signed-in users with their own quota

### 9.3 Provider abstractions

Both `LLMProvider` and `Embedder` are Python Protocols with stable interfaces. New providers are added by implementing the Protocol and registering with a factory keyed off env variables. No provider-specific code in the retrieval or query pipelines — they consume the abstraction, not a concrete implementation. This is what makes the ablation studies (§8) clean to run.

### 9.4 Citation accuracy

Cited answers are only as valuable as the citations are trustworthy. A hallucinated file path or a citation to a function that doesn't exist destroys user trust faster than a wrong answer — users learn to distrust the whole product after a single bad citation. This subsection documents how we prevent that failure mode.

**Threat model.** LLMs hallucinate citations in three distinct modes, each requiring a different mitigation:

1. **Fabricated identifiers** — citing `auth/login.py:142` when no such file or line exists
2. **Real but irrelevant** — citing a real file that has nothing to do with the claim
3. **Stale or shifted** — citing line ranges that have moved since indexing

We address (1) and (3) mechanically. (2) requires semantic judgment and is addressed via evaluation.

**Approach 1: Constrained citation IDs.**

Each retrieved chunk passed to the LLM has a stable `chunk_id` (short hash, e.g. `c7f3a1`). The system prompt instructs the LLM to cite using these IDs only, in the form `[chunk:c7f3a1]` — never free-form `file:line` strings. This eliminates fabricated-identifier hallucinations by construction: a citation either references a chunk that was actually in the retrieval set, or it fails parsing.

We chose constrained IDs over free-form citations with post-hoc lookup because the latter still allows hallucination through and only catches it after the fact. Constraining at generation time is more reliable. The cost is prompt complexity (the LLM must understand the ID format) and slightly higher token count for chunk metadata in context — judged worthwhile.

**Approach 2: Post-hoc validation and resolution.**

Before returning an answer to the user, the system:

1. Parses all `[chunk:...]` tokens from the LLM output
2. Verifies each token references a chunk that was in the retrieval set for this query
3. Resolves each valid chunk_id to its file path, line range, and a permalink (using the **commit SHA pinned at ingestion time**, not `main`)
4. For any invalid token: flag and degrade (UI shows "this claim has no verified source") rather than silently strip — silent stripping presents an unsourced claim as sourced, which is worse than acknowledging the gap

**Approach 3: Explicit "no source" sentinel.**

If the LLM wants to make a claim no retrieved chunk supports, the prompt instructs it to either omit the claim or attach `[chunk:none]`, which renders as "uncited — verify independently." This is preferred over forcing the model to invent a citation.

**Measuring citation accuracy.**

Two metrics are tracked separately:

- **Mechanical accuracy**: fraction of citation tokens that parse to a valid chunk_id from the retrieval set. Target ≥ 99%. Anything less indicates a prompting or parsing bug.
- **Semantic accuracy**: fraction of citations that human review judges to be *relevant* to the claim being cited. Target ≥ 90%. Measured on the eval set with a documented rubric; spot-checked LLM-as-judge for ongoing tracking between manual reviews.

**Risk: refactored code.**

For repos with significant refactoring history, citations may point to current code that no longer matches the historical PR discussion the answer references. For `historical_why` answers, the UI distinguishes between "current code (as of indexed commit X)" and "historical context (PR #Y from N months ago)," making it explicit when these may diverge. Documented as a known limitation in the README.

### 9.5 Multi-hop retrieval for "why" questions

Single-vector retrieval works for "where" and "how" questions (lookups, code understanding) but breaks down for "why" questions, which require traversing structured relationships across the repo:

> "Why does this function take a context object instead of individual parameters?"

A correct answer requires finding (a) the function in question, (b) the commit that introduced or last meaningfully modified it, (c) the PR that contained that commit, (d) discussion threads within that PR, and possibly (e) related issues. No single vector search recovers this chain.

**Approach: graph-augmented retrieval.**

We model the repo as a typed graph in Postgres (no separate graph DB needed for v1):

- **Nodes**: code chunks, commits, PRs, issues, files
- **Edges**: `chunk -[introduced_by]-> commit`, `commit -[part_of]-> pr`, `pr -[closes]-> issue`, `chunk -[in_file]-> file`, plus inverses

Edges are populated at ingestion time from git blame data and the GitHub GraphQL API. The graph lives in standard Postgres tables; traversal is via recursive CTEs.

**Retrieval pipeline for "why" questions.**

1. **Query classification.** A lightweight classifier (initially a prompted LLM call; potentially a fine-tuned small model in v1.1 if cost demands it) categorizes the query as `lookup` / `architectural` / `historical_why` / `impact`. Different categories trigger different retrieval strategies.

2. **For `historical_why` queries:**
   - Step 1: hybrid retrieval finds the code region(s) the question is about (top-k chunks via the standard pipeline)
   - Step 2: for each top chunk, traverse `chunk → introducing_commit → containing_pr → linked_issues` to expand the candidate set
   - Step 3: re-rank the expanded set by relevance to the original query (cross-encoder reranker over both code chunks and PR/issue text)
   - Step 4: pass the top-k post-expansion results to the LLM with their relationships annotated

3. **For other categories:** standard hybrid retrieval, no graph traversal — keeps latency low for the majority of queries.

**Why not just retrieve commit messages too?**

Naively embedding commit messages and PR descriptions and searching across all three sources (code + commits + PRs) works for simple cases but misses the *structural* connection between them. Embedding similarity might surface a commit about authentication when asked about authentication code, but it won't reliably connect *this specific function* to *the commit that introduced it*. The graph traversal makes that connection explicit.

**Evaluation.**

The `historical_why` subset of the eval (~25% of questions) measures this pipeline specifically. Recall@5 is reported separately for `historical_why` vs. other categories, since they exercise different retrieval paths. The ablation in §8 compares: (a) flat retrieval across code+commits+PRs (naive), (b) flat retrieval plus graph expansion, (c) full pipeline with query classification + selective graph traversal. The delta between (a) and (c) is the measured value of multi-hop retrieval.

---

## 10. Risks and mitigations

- **Risk**: Ingestion takes too long, users bounce. **Mitigation**: Progressive availability + pre-indexed popular repos + clear progress UI.
- **Risk**: Hallucinated answers destroy trust. **Mitigation**: Constrained citation IDs + post-hoc validation (§9.4); citation accuracy is a tracked metric.
- **Risk**: Cost runs away with public traffic. **Mitigation**: Rate limits, pre-indexed cache hits, BYO-key tier, hard daily cost cap with circuit breaker.
- **Risk**: Eval set is too small or biased. **Mitigation**: Tiered eval (hand-curated + auto-generated), documented methodology, transparent reporting of limitations.
- **Risk**: GitHub rate limits during ingestion. **Mitigation**: GraphQL batched queries, authenticated requests, aggressive caching, retry with backoff.
- **Risk**: Refactored code makes historical citations confusing. **Mitigation**: UI distinguishes "current code" from "historical context" for `historical_why` answers; commit-SHA-pinned permalinks (§9.4).
- **Risk**: Query classification is wrong, sending a "why" question down the flat retrieval path. **Mitigation**: Classifier accuracy tracked on the eval set; misclassifications surface in error analysis. Default fallback is to run *both* pipelines and merge top-k, accepting the latency cost, if classifier confidence is low.
- **Risk**: Scope creep delays shipping. **Mitigation**: Strict non-goals list; defer anything not in v1 to `docs/roadmap.md`.

---

## 11. Milestones

- **Week 1**: Project scaffold, Postgres + pgvector via docker-compose, ingestion pipeline (clone + file metadata + tree-sitter chunking), embedding pipeline with `Embedder` abstraction, naive vector search endpoint. Deployed dev environment on Railway with placeholder frontend.
  *(Note: a prototype of naive RAG was built locally before this PRD was finalized. Week 1 is the production-quality re-implementation with proper abstractions, not greenfield work. Without the prototype experience, this scope would span 2 weeks.)*
- **Week 2**: LLM provider abstraction, citation ID scheme + post-hoc validation (§9.4), streaming answers in the UI, citation rendering with Monaco.
- **Week 3**: Commit + PR ingestion, graph edges, query classifier, multi-hop retrieval for `historical_why` (§9.5).
- **Week 4**: Evaluation set built (flagship hand-curated + auto-eval pipeline), ablation studies run, results in README.
- **Week 5**: Progressive ingestion UX, pre-indexed popular repos, suggested questions, feedback widget, low-confidence UI, out-of-scope handling, polish.
- **Week 6** (buffer): Public deployment, custom domain, write-up (README, blog post, demo video).

---

## 12. Success criteria

The v1 is successful if:

- Live public URL with at least 5 pre-indexed flagship repos working flawlessly
- Quantitative eval results published in the README, broken out by question category and ablation
- At least one ablation shows a measurable improvement from a non-obvious technical choice (e.g., AST chunking, or multi-hop retrieval on `historical_why` subset)
- Auto-eval pipeline demonstrably works on a second repo, with reported numbers
- Citation accuracy: ≥ 99% mechanical, ≥ 90% semantic on the flagship eval
- At least 10 real users have tried it and submitted feedback

---

## 13. Open questions

- Whether to support a "BYO-key, private repo" tier in v1.5 (between v1's public-only and v2's full private support) if user demand emerges
- Whether to publish the auto-eval pipeline as a standalone open-source library (`codecontext-eval`). Strong candidate for v1.1 — see roadmap.
- Whether the query classifier should be replaced with a routing-by-keywords approach as a simpler baseline, with the LLM classifier reserved for ambiguous cases

---

## Appendix: Document history

- **v2** (current): Incorporates technical review feedback. Adds §9.4 (citation accuracy), §9.5 (multi-hop retrieval), §8.1 (flagship rationale), §9.2 (GitHub auth). Fixes the LOC/files cap logic in §6.1. Clarifies recall and latency targets in §7. Splits Week 1 milestone for realism. Adds out-of-scope and low-confidence UI handling to §6.2. Adds refactored-code risk and query-classifier risk to §10.
- **v1**: Initial draft.
