# PRD: CodeContext — Conversational Code Intelligence for GitHub Repos

Author: Amrutha
Status: Draft v1
Last updated: May 2026

## 1. Summary

CodeContext is a web application that lets developers ask natural-language questions about any public GitHub repository and get accurate, cited answers grounded in the repo's code, commit history, pull requests, and issues. It retrieves from real repository data and cites its sources, enabling questions that traditional tools can't answer — particularly *why* questions about historical decisions.

The v1 goal is a deployed, demoable web product with rigorous evaluation on at least one flagship repo, supporting on-demand ingestion of any public repo within scope limits.

## 2. Problem

Engineers joining a new codebase, contributing to an open-source project, or debugging an unfamiliar system spend significant time reconstructing context that already exists somewhere in the repo's history. Specifically:

- Code search tools (grep, GitHub search, IDE search) find symbols but not intent.
- Generic AI chatbots hallucinate code that doesn't exist or describe codebases they don't actually know.
- Documentation is incomplete, outdated, or doesn't cover historical "why" questions.
- Asking a teammate requires their availability and assumes they remember.

The "why" information exists — it's in commit messages, PR descriptions, issue threads, and code review comments — but it's not searchable in any practically useful way.

## 3. Goals and non-goals

### Goals

- Allow users to ask natural-language questions about any public GitHub repo and receive answers grounded in actual repo data
- Cite all claims with clickable references to specific files, lines, commits, PRs, or issues
- Support the four most useful question categories: factual lookup, architectural understanding, historical/intent ("why"), and impact analysis ("what depends on this")
- Deliver demonstrable retrieval quality on at least one flagship repo with quantitative evaluation
- Ship as a publicly deployed web app with a live URL

### Non-goals (explicitly out of scope for v1)

- Private repo support (avoids OAuth-scope complexity, privacy/legal surface, and storage liability)
- Code generation, editing, or PR drafting (this is a retrieval product, not a Copilot competitor)
- Real-time index updates (manual refresh button is sufficient)
- Multi-repo / cross-repo queries (single repo per session)
- IDE integration (web only)
- Mobile-optimized UI (desktop-first; mobile is best-effort)

## 4. Target users

**Primary:** Software engineers exploring an unfamiliar open-source codebase — to evaluate it, contribute to it, or learn from it.

**Secondary:** Engineers researching how a specific problem has been solved in well-known OSS projects (e.g., "how does FastAPI handle dependency injection?").

**Tertiary:** Engineering students and bootcamp graduates studying production codebases for learning.

**Not targeted in v1:** Enterprise teams analyzing private codebases (requires a different product surface, auth model, and trust posture).

## 5. User stories

- As a developer evaluating a new OSS library, I want to ask "how does this library handle X?" and get a grounded answer with citations, so I can decide whether to adopt it without reading the whole codebase.
- As a new contributor to a project, I want to ask "why was this designed this way?" and see the historical PRs and discussions, so I understand the rationale before suggesting changes.
- As an engineer debugging a third-party dependency, I want to ask "what changed in version X.Y?" and see specific commits and PRs, so I can isolate the source of a regression.
- As a learner, I want to ask "show me an example of how authentication is implemented" and get real code from real projects, so I can study patterns in production code.

## 6. Functional requirements

### 6.1 Repo ingestion

- Users can ingest any public GitHub repo by URL or by selecting from a list of pre-indexed popular repos
- Ingestion limits: max 200,000 lines of code or 5,000 files (whichever is lower); larger repos receive a clear error with a suggestion to specify a subdirectory
- Ingestion pipeline indexes: source code (AST-chunked), commit history (last 12 months by default, full history opt-in), merged PRs, closed issues, and the README/docs directory
- Progressive availability: file structure browsable within 10s, symbol lookups within 30s, semantic search within 2min, full historical queries within 5min
- Progress is shown to the user with stage-level indicators

### 6.2 Querying

- Single text input for natural-language questions
- Streaming token output as the LLM generates
- Every factual claim in the answer is wrapped in a citation linking to a specific source (file+line range, commit SHA, PR number, or issue number)
- Citations expand inline to show the relevant snippet without leaving the page; "open on GitHub" link for full context
- A collapsible "sources retrieved" panel shows which documents the retrieval surfaced, with relevance scores
- Suggested starter questions are shown for each repo (auto-generated from repo structure)

### 6.3 Conversation

- Follow-up questions within a session use prior context
- Conversation history is preserved per repo per session (browser-local for v1; account-bound is v2)
- "Clear conversation" and "new chat" actions

### 6.4 Feedback

- Thumbs up / thumbs down on every answer
- Optional free-text feedback on thumbs-down
- Feedback is stored with the query, retrieved sources, and generated answer for later analysis

### 6.5 Repo management

- "Refresh repo" button to re-ingest (rate-limited to once per 24 hours per repo)
- Repos auto-expire from the index after 14 days of no queries; users can re-ingest on demand
- A "popular repos" page shows pre-indexed repos for instant access

### 6.6 Cost controls

The default deployment uses free-tier providers (Gemini 2.0 Flash for the LLM, local `bge-base-en-v1.5` for embeddings — see §9 and ADR 0007). The per-tier limits below exist primarily to protect the *shared* Gemini free-tier quota from being exhausted by one user, not to manage paid-API spend on the default path.

- **Anonymous users:** 20 queries per day. Can only query pre-indexed popular repos.
- **Signed-in users** (GitHub OAuth, public repo scope): 50 queries per day. Can ingest any in-scope repo.
- **BYO-provider users:** bring your own `LLMProvider` configuration (OpenAI, Anthropic, Gemini paid tier, or self-hosted Ollama). Unlimited queries against your own quota; you pay your own provider's cost.
- All limits visible and explained in-app.

## 7. Non-functional requirements

- Query latency: p50 < 3s, p95 < 6s (end-to-end including LLM generation)
- Ingestion latency: 200k LOC repo fully indexed in < 5 minutes
- Retrieval quality: recall@5 ≥ 0.75 on the flagship-repo evaluation set
- Citation accuracy: ≥ 90% of citations point to real, relevant locations
- Uptime: best-effort 99% (this is a portfolio project, not an SLA product)
- Cost ceiling: total infra + API cost per active user per month < $0.10 at expected v1 traffic. Achievable because the default `LLMProvider` (Gemini Flash free tier) and `Embedder` (local `bge-base-en-v1.5`) are zero-marginal-cost; remaining cost is small infra (Railway / Fly free or near-free tier). The $0.10 ceiling is defensive in case any portion of the path escapes to a paid provider.

## 8. Evaluation plan

The evaluation framework is a first-class deliverable, not an afterthought. It's how we know the system works and how we make engineering decisions.

**Flagship-repo eval:** 60–80 hand-curated questions on one repo (FastAPI proposed). Mix of: factual lookup (~30%), architectural (~25%), historical/intent (~25%), impact analysis (~20%). Each question has labeled ground-truth sources and an expected-answer rubric.

**Auto-generated eval (Tier 1):** Pipeline that, given any new repo, generates ~50 evaluation questions automatically from: PR descriptions ("Why was X changed?"), closed issues linked to fix PRs ("How was Y fixed?"), and AST-derived symbol lookups ("Where is Z defined?"). Used to validate generalization to repos beyond the flagship.

**Metrics tracked per release:**

- Retrieval recall@5 and recall@10
- Citation accuracy (% of citations that point to real, relevant locations)
- Answer quality (LLM-as-judge with documented rubric; spot-checked by hand on a 20-question sample)
- p50 / p95 latency
- Cost per query

**Ablation studies to be reported in the v1 README:** AST vs. naive chunking, vector-only vs. hybrid retrieval, with reranker vs. without, OpenAI vs. open-source embeddings. Each ablation reports the delta on recall@5.

## 9. Technical architecture

**Backend**: Python `>=3.12` (currently 3.14 via uv-managed install) + FastAPI. `uv` for dependency management. Async ingestion workers via background tasks (RQ or arq if we outgrow background tasks).

**Storage**: Postgres with pgvector for both structured data (repos, files, commits, PRs, issues) and vector embeddings (code chunks, commit messages, PR/issue text). Single-database simplicity for v1.

**Parsing**: tree-sitter (multi-language) for AST extraction and chunking. Languages supported in v1: Python, TypeScript/JavaScript, Go, Rust (in that priority order).

**Embeddings**: `bge-base-en-v1.5` via `sentence-transformers`, running in-process in the Python backend on CPU. No API dependency, no cost, fast enough for v1 corpus sizes. Embedding model is abstracted behind an `Embedder` interface so it can be swapped (e.g., to OpenAI `text-embedding-3-small`, Voyage `voyage-code-2`, or Ollama `nomic-embed-text`) for the ablation study.

**LLM**: Gemini 2.0 Flash via the free tier as the default. Accessed via the OpenAI-compatible SDK pattern (Google's `openai`-compatible endpoint or a thin adapter) so the same client code works across providers. The LLM is abstracted behind an `LLMProvider` interface with these implementations:
- `gemini` (default, free tier)
- `openai` (GPT-4o-mini, paid, used for ablation)
- `anthropic` (Claude Haiku/Sonnet, paid, used for ablation)
- `ollama` (local Qwen 2.5 Coder 3B Instruct, CPU inference, used for offline development and ablation)

Provider selected via `LLM_PROVIDER` env variable. All providers must support streaming.

**Retrieval**: Hybrid — Postgres full-text search (BM25-equivalent via `ts_rank`) plus pgvector cosine similarity, fused with reciprocal rank fusion. Optional cross-encoder reranker (`bge-reranker-base`, CPU) on top-k results.

**Frontend**: Next.js `>=14` (currently 16), React 19, Tailwind 4, TypeScript strict. App Router. Monaco for code rendering in citations. Server-sent events for streaming answers.

**Deployment**: Frontend on Vercel, backend on Railway or Fly.io, Postgres on Neon or Supabase. Custom domain.

**Observability**: Basic structured JSON logging, query/feedback events to Postgres, no third-party analytics in v1.

**Dev environment notes**: Primary dev machine is Windows 11 on an AMD Ryzen 5 7535HS (integrated Radeon 660M, 16GB RAM). All local inference runs on CPU; no GPU acceleration assumed. Ollama is used for offline development and as one ablation data point — not as the production default.

## 10. Risks and mitigations

- **Risk:** Ingestion takes too long, users bounce. **Mitigation:** Progressive availability + pre-indexed popular repos + clear progress UI.
- **Risk:** Hallucinated answers destroy trust. **Mitigation:** Citation verification step (validate that cited files/lines exist before returning); citation accuracy is a tracked metric.
- **Risk:** Cost runs away with public traffic. **Mitigation:** Rate limits, pre-indexed cache hits, BYO-key tier, hard daily cost cap with circuit breaker.
- **Risk:** Eval set is too small or biased. **Mitigation:** Tiered eval (hand-curated + auto-generated), documented methodology, transparent reporting of limitations.
- **Risk:** GitHub rate limits during ingestion. **Mitigation:** GraphQL batched queries, authenticated requests, aggressive caching, retry with backoff.
- **Risk:** Scope creep delays shipping. **Mitigation:** Strict non-goals list; defer anything not in v1 to a written v2 backlog.

## 11. Milestones

- **Week 1:** Ingestion pipeline (GitHub API → Postgres), naive RAG end-to-end on FastAPI, deployed dev environment
- **Week 2:** AST chunking, hybrid retrieval, citation rendering in the UI, streaming answers
- **Week 3:** Evaluation set built (flagship + auto-eval pipeline), ablation studies run, results in README
- **Week 4:** Progressive ingestion UX, pre-indexed popular repos, suggested questions, feedback widget, polish
- **Week 5 (buffer):** Public deployment, custom domain, write up the project (README, blog post, demo video)

## 12. Success criteria

The v1 is successful if:

- Live public URL with at least 5 pre-indexed flagship repos working end-to-end, with known limitations documented
- Quantitative eval results published in the README (recall@5, citation accuracy, latency, costs)
- At least one ablation study showing a measurable improvement from a non-obvious technical choice (e.g., AST chunking)
- Auto-eval pipeline demonstrably works on a second repo, with reported numbers
- At least 10 real users (friends, classmates, online community) have tried it and submitted feedback

## 13. Open questions

- Whether to add a simple "chat with multiple repos" mode in v2 (cross-repo retrieval is hard but valuable)
- Whether to publish the auto-eval pipeline as a standalone open-source tool (could be a second resume artifact on its own)
