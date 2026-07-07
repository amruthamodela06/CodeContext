# CodeContext — Roadmap

**Author:** Amrutha
**Last updated:** July 2026 (Slice 6 complete — hybrid retrieval + RRF + optional reranker)
**Companion document to:** `docs/PRD.md` (v1)

This document captures everything *not* in v1 — what's coming next, what's speculative, and what we've explicitly decided not to do. It exists so that the v1 PRD can stay focused on v1, and so that scope creep into v1 can be redirected here without the idea being lost.

**How to use this doc:**
- If you find yourself wanting to add something to v1, check whether it's already here. If yes, leave it here. If no, add it here first, then decide whether it belongs in v1.
- Items here are not commitments. They are candidates, scoped at a level of precision proportional to how near-term they are.
- v1.1 items have rough scoping. v2 items have one-paragraph descriptions. v3+ items are one-liners.

---

## Status legend

- 🎯 **Committed** — planned to ship, scoped, has a rough timeline
- 🟡 **Likely** — strong intent, scope not yet finalized
- 🔵 **Candidate** — under consideration, depends on v1 reception or own interest
- ⚪ **Speculative** — interesting if it happens, no plan
- ❌ **Won't do** — explicitly rejected, with reasoning

---

## v1.1 — Visibility and polish

**Target**: 2–4 weeks after v1 ships.

**Theme**: Amplify the work that v1 already did. Make the project discoverable, extractable, and more credible. Don't add major new features — that's v2's job.

### 🎯 Open-source the auto-eval pipeline as `codecontext-eval`

The auto-eval pipeline built for v1 (PR-derived, issue-derived, and AST-derived question generation for any GitHub repo) is repackaged as a standalone Python library, independent of CodeContext itself.

**Why this is high-priority:**
- It's a genuinely novel contribution. There isn't a standard code-RAG eval framework in the OSS ecosystem.
- It's a much smaller artifact than the full product, so it's actually completable as a side deliverable.
- "Maintainer of an open-source eval framework" reads more strongly to recruiters than "I built a chatbot."
- It can attract academic citations and OSS contributors in a way the product can't.

**Rough scope:**
- Extract the auto-eval logic from `eval/` into a separate `codecontext-eval/` package
- Clean up the public API (a `CodeContextEval(repo_url).generate()` interface, returning an `EvalDataset` object)
- Document the methodology for each question type
- Publish to PyPI with semantic versioning
- Write a README that stands alone (not assuming knowledge of CodeContext)
- Submit to relevant venues: r/LocalLLaMA, Hacker News, the Hugging Face datasets community

### 🟡 Technical blog post / writeup

A single long-form blog post covering:
- The problem (why code-RAG is harder than document-RAG)
- The technical choices (AST chunking, hybrid retrieval, citation IDs, multi-hop for "why" questions)
- The ablation results, with charts
- What surprised me, what I'd do differently

Posted on a personal domain, cross-posted to dev.to or Medium. This is the artifact that connects the project to a broader audience.

### 🟡 Expand pre-indexed repo set

Grow from the 5 launch repos to ~20, covering a wider language and domain mix. Driven by user requests and demo-readiness, not arbitrary growth.

### 🔵 Address top user feedback themes from v1

After 2–4 weeks of real users, certain pain points will be obvious. Fix the top 3–5. This is an explicit slot for "real user feedback" rather than my own priorities, which is otherwise easy to under-prioritize.

### 🔵 Simple CLI for power users

`codecontext query --repo tiangolo/fastapi "how does dependency injection work?"` for users who'd rather not use the web UI. Low effort if the backend is well-abstracted; meaningful UX win for the right audience.

---

## v2 — Private repos and accounts

**Target**: A future major version. Timing depends on v1 traction and personal interest.

**Theme**: Make CodeContext useful for the user's *own* code, not just public OSS. This is a meaningfully different product surface — it requires real authentication, a privacy posture, persistent user state, and a different deployment model.

### 🟡 GitHub OAuth with persistent accounts

- OAuth flow with `public_repo` scope (default) and `repo` scope (opt-in for private repos)
- Account-bound conversation history and feedback
- Per-user query quotas
- Account settings page (manage repos, API keys, data export, delete account)

### 🟡 Private repo ingestion

- Support indexing repos the authenticated user has access to
- Encryption at rest for indexed content
- Documented data retention and deletion policies
- Clear policy: indexed content is not used for any training, never shared, deletable on request
- Per-user isolation: queries on a private repo only return chunks from that user's accessible repos

This is the largest scope expansion in the roadmap because it requires getting privacy, auth, and isolation right. Worth its own dedicated planning cycle before being committed.

### 🟡 Real-time index updates via webhooks

- Register a GitHub webhook on indexed repos
- On push / new PR / new issue: incrementally update the affected chunks, embeddings, and graph edges
- Replace the "manual refresh" model from v1 for repos the user has admin access to
- Public-repo pre-indexed set continues to use scheduled refreshes (no webhook needed)

### 🔵 Cross-repo retrieval (single user, multiple repos)

- "Search across all my indexed repos" mode
- Useful for engineers in monorepo-adjacent setups, or for "I know I solved this once, where was it?" questions
- Retrieval and citation work mostly unchanged; UI needs a multi-repo source rendering mode

### 🔵 Migrate to GitHub App

- Higher per-installation rate limits
- Cleaner installation UX (one click vs. OAuth scope acceptance)
- Foundation for any future GitHub Marketplace presence

### 🔵 Per-user API keys for the BYO-key tier

Persistent storage of user-provided keys (OpenAI, Anthropic, Voyage) so they don't re-enter them every session. Requires careful encryption and key-handling discipline.

---

## v3 and beyond — Speculative product directions

**Theme**: Where CodeContext *could* go if it gains traction. None of these are commitments; they're directions that would make sense if the foundation succeeds.

### ⚪ Code generation grounded in repo context

The natural extension of "ask questions" is "ask for changes." A `codecontext edit "add rate limiting to the login endpoint"` flow that retrieves relevant context, drafts a diff, and explains its reasoning with the same citation discipline as v1.

This is competitive with Cursor / Copilot Workspace / Aider and would require careful positioning. Probably only worth pursuing if CodeContext's *retrieval* quality is meaningfully better than alternatives — otherwise it's just another agent.

### ⚪ Team analytics layer

Repo-level insights about a team's work patterns: knowledge concentration (which files only one person has touched), hotspots (high-churn + high-complexity files), onboarding difficulty estimates, abandoned subsystems. Builds on the graph data CodeContext already has.

This is a different product than "answer engine" — closer to CodeScene or LinearB. Worth considering but should probably be a separate product, not an addition to CodeContext proper.

### ⚪ IDE integration (VS Code, JetBrains)

A side panel that surfaces CodeContext answers within the editor, with context auto-derived from the file/symbol currently open. Lower-friction than going to a web UI.

Technically straightforward (the backend doesn't change); user adoption is the harder question. Worth doing only if the web product has meaningful traction first.

### ⚪ Enterprise tier

SSO, audit logs, on-prem deployment, custom data retention policies, dedicated support. Standard enterprise SaaS feature set. Only relevant if the project becomes a real product with real revenue, which is not the current goal.

### ⚪ Native mobile apps

iOS and Android clients. Probably not — the use case isn't mobile-first, and the dev cost is high. Listed here to be explicit that I've considered and (currently) rejected it.

### ⚪ Slack / Discord bot integration

`/codecontext how does our auth work?` in a team Slack. Cute, niche, low effort if the public API is in good shape. Probably better as a community contribution than a first-party feature.

---

## ❌ Won't do — explicit non-goals

These are things that come up naturally in conversation about CodeContext but that we have actively decided not to pursue. Listed here so they don't get re-litigated.

### Mobile-first UI

The use case — exploring a codebase, reading long answers with code citations — is fundamentally desktop. A responsive web app that works on mobile is fine; a native mobile app is not worth building. Decided at v1.

### Generic chatbot or "AI assistant"

CodeContext is specifically a *repo-grounded* tool. The value comes from retrieval + citation discipline. Becoming a generic chatbot dilutes the focus and competes with products that have orders of magnitude more resources. Decided at v1.

### Replacement for code search

CodeContext is an *answer engine* layered on top of code search, not a competitor to grep, ripgrep, or GitHub's own search. Users who want fast literal-string search should use those tools. CodeContext's job starts where their job ends. Decided at v1.

### Code review automation

"Have CodeContext review this PR" sounds tempting but is a substantially different product (it requires being good at adversarial code reading, not retrieval) and competes with established tools. Out of scope at all foreseeable versions.

### Marketplace / monetization

This is a portfolio project, not a startup. Free tier with rate limits + BYO-key for power users is sufficient. Monetization would change the project's character in ways that aren't aligned with its goals.

### "Multi-agent" anything

No agent loops, no autonomous research, no "AI does the work for you." CodeContext retrieves and answers; humans decide what to do with the answer. This is a deliberate design stance, not a temporary limitation.

---

## Process notes

### When to move items between sections

- 🔵 Candidate → 🟡 Likely: when scope is roughly understood and there's clear intent to build
- 🟡 Likely → 🎯 Committed: when there's a timeline and a plan
- Anything → ❌ Won't do: when I've actively decided against it (and document why)
- ❌ Won't do → anywhere else: requires explicit justification in a commit message

### When to add new items

Anything that comes up in conversation, code review, or my own thinking that isn't v1 goes here. Items can be added casually as ⚪ Speculative; promoting them to higher status requires a real reason.

### When this document is reviewed

- After v1 ships: full review, re-prioritize v1.1 based on what was learned
- Quarterly: light review, drop ⚪ items that no longer feel interesting
- Before each major version: full review and re-scoping

---

## Appendix: Items moved out of v1 during PRD review

Documenting items that were considered for v1 and explicitly deferred, so the reasoning is preserved:

- **Account-bound conversation history**: deferred to v2 because it requires auth, which v1 doesn't have. Browser-local history is sufficient for the v1 use case.
- **Private repo support**: deferred to v2 (primary v2 theme). Adding it to v1 would expand scope by ~50%.
- **Cross-repo queries**: deferred to v2. Useful but not core to the v1 thesis (proving the core idea on a single repo).
- **GitHub App authentication**: deferred to v2. Personal access token is sufficient for v1's pre-indexing strategy.
- **`codecontext-eval` as standalone library**: deferred to v1.1. The pipeline itself ships in v1; only the *packaging* as a separate library moves to v1.1.
- **Real-time updates via webhooks**: deferred to v2. Manual refresh button is sufficient for v1.
