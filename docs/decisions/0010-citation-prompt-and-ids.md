# ADR 0010: Citation prompt, display IDs, and mechanical verification

**Status**: Accepted (one-shot example added 2026-05-30 after observed 3B-model behavior — see Iteration log)
**Date**: 2026-05-30

## Context

Slice 4 adds the first LLM-generated answers (`POST /query`). PRD §9.4 requires that every answer be grounded in retrieved code with **mechanically verifiable** citations — not "the model claims a source," but "the system proved the cited code exists and resolved it to a file/line/permalink." This is a load-bearing trust property for the whole product: answers without verifiable citations are indistinguishable from a generic chatbot guessing.

Three things need to be settled and tracked: (1) how the model is told to cite, (2) the on-the-wire citation token format, and (3) how citations are parsed and validated after generation. The prompt is a real engineering artifact we will iterate on, so it lives here rather than buried in a Python string.

This ADR builds on the `LLMProvider` interface (ADR 0007, Slice 4 amendment) and the vector retrieval from ADR 0009.

## Decision

### Display IDs: `[chunk:c1]`, sequential per query

Each retrieved chunk gets a short **display ID** — `c1, c2, … cN` in retrieval-rank order — assigned fresh per query. The model cites with the inline token `[chunk:c1]`; the no-support sentinel is `[chunk:none]`.

- **Sequential, not hash-based.** Citations are query-scoped, so cross-query ID stability buys nothing. Sequential IDs are predictable and low-token, which makes the model emit them reliably; a hash like `a3f` is more likely to be mistyped (`a3e`), inflating the invalid-citation rate.
- **`c` prefix, not a bare number.** `[chunk:1]` collides visually with markdown footnotes (`[1]`) and ordered-list markers, making the parser ambiguous. The `c` prefix keeps the token unambiguous.

The full `chunk_id` (the integer PK from Slice 2) is retained in the citation context and is what the resolved citation carries back to the client; the display ID is only the LLM-facing/user-facing handle.

### Prompt template (canonical form — iterate here and in `app/citations/prompt.py` together)

System message:

```
You are CodeContext, a code-intelligence assistant answering questions about the
GitHub repository {owner}/{name}. Answer using ONLY the code excerpts provided.
Each excerpt has a short ID (c1, c2, ...).

Rules:
1. Cite every factual claim with the supporting excerpt ID, written as [chunk:c1],
   placed at the END of the clause it supports. Multiple supporting excerpts:
   [chunk:c1][chunk:c2].
2. Do not state claims the excerpts don't support. If a statement is unavoidable
   but unsupported, mark it [chunk:none].
3. If the excerpts don't contain enough to answer, say so plainly -- do NOT answer
   from general knowledge.
4. Only use IDs that appear in the excerpts below. Never invent IDs.
5. Show code in fenced blocks. NEVER put a [chunk:...] tag inside a code block or
   inline code span -- citations belong in prose only.
6. Be concise and specific (function names, file paths).
```

User message:

```
Question: {question}

Code excerpts:
[c1] {file_path}:{start}-{end} ({chunk_type} {name})
```{language}
{content}
```

[c2] ...
```

Rule 3 enforces the §6.2 out-of-scope behavior (refuse rather than hallucinate). Rule 5 plus the parser's code-skipping (below) are belt-and-suspenders against literal `[chunk:...]` text inside code confusing the parser.

### Parse → validate → resolve

Three separable steps, in `app/citations/`:

1. **Parser** (`parser.py`) — a line-oriented state machine tracks fenced code blocks (` ``` ` and `~~~`, variable length; a longer fence isn't closed by a shorter one; an unterminated fence suppresses the rest and emits a `fence_unterminated` warning) and blanks inline code spans, then extracts tokens with a **shape-only** regex `\[chunk:(none|[A-Za-z0-9_-]{1,16})\]`. The parser deliberately does **not** check whether an ID is real — it surfaces well-formed-but-unknown IDs so they can be flagged, because a silently dropped hallucination is worse than a visibly flagged one.
2. **Validator** (`validator.py`) — membership in the per-query context is the ground truth. Each parsed ID is classified `valid` (maps to a retrieved chunk), `none` (the explicit sentinel), or `invalid` (unknown). De-duplicated by display ID.
3. **Resolve** — `valid` citations resolve to `{chunk_id, file_path, start_line, end_line, permalink}`.

### Permalinks are commit-SHA-pinned (§9.4)

Permalinks use the HEAD commit SHA captured at ingestion time, not a branch ref:
`https://github.com/{owner}/{name}/blob/{commit_sha}/{path}#L{start}-L{end}`. A branch ref (`main`) drifts as the repo changes, so a "permalink" to `main` could point at unrelated code later. This adds a nullable `repo.commit_sha` column (migration `b1f4c2a09d31`); repos ingested before the column fall back to the branch ref.

### Streaming protocol (SSE)

`POST /query` returns `text/event-stream` via `sse-starlette`. Event order: `sources` (all retrieved chunks, up front — powers the Sources panel and supplies chunk bodies for citation expansion) → `token` × N (answer deltas, forwarded **and** accumulated server-side) → `citations` (resolved list + warnings) → `done`. Any LLM failure mid-stream emits an `error` event (with `stage`) instead of silently truncating. On client disconnect the generator is cancelled and the provider's `finally` closes the upstream LLM stream (no leaked connection / burned quota). A `stream=false` fallback returns the same data as one JSON blob.

## Consequences

**Upside**

- Citations are verifiable by construction: the response can only contain file/line/permalink for IDs that were actually retrieved. Hallucinated IDs are surfaced as `invalid`, never silently honored.
- The prompt is a tracked artifact; prompt iterations are diffable here and in one rendering module.
- Sequential IDs minimize tokens and emission errors.
- The parser is unit-tested against the nasty cases (code fences, inline spans, malformed/unterminated tokens, variable-length fences) independently of any LLM.

**Costs / limitations**

- **Semantic mis-citation is not caught.** The mechanism proves a cited ID maps to a real chunk; it does **not** prove the chunk semantically supports the specific claim (the model could cite `c3` where `c1` is the real support). This is an accepted v1 limitation — semantic citation accuracy is measured in the Slice 7 eval (LLM-as-judge + hand-checked sample), not enforced at query time. Detecting it mechanically (e.g. per-sentence embedding similarity) was considered and deferred: more surface, more false positives, and it belongs in eval.
- Display IDs aren't stable across queries — fine for single-turn answers, but a future "cite the same chunk across a conversation" feature would need the stable `chunk_id`, which we already retain.
- Plain-text answer rendering linkifies every `[chunk:cX]` token, including any the model wrongly placed inside a code block (rare given rule 5). Acceptable for v1; the rigorous layer is the validated citation list + Sources panel.

**Rejected alternatives**

- *Hash-based stable IDs* — stability is irrelevant for query-scoped citations and raises the mistype/invalid rate.
- *Bare-number IDs (`[1]`)* — ambiguous against markdown footnotes/list markers.
- *Single-regex parser with no fence tracking* — can't reliably tell whether a token sits inside a code block without stateful delimiter tracking; breaks on multi-line/variable-length fences.
- *Branch-ref permalinks* — simpler but drift over time, violating the "permalink" contract in §9.4.
- *Constrained decoding / function-calling to force valid IDs* — heavier, provider-specific, and still needs post-hoc validation anyway; the mechanical validator is the ground truth regardless.

**Re-evaluation triggers**

- If the invalid-citation rate on the eval set is high, revisit the prompt (few-shot examples) before changing the ID scheme.
- If semantic mis-citation proves common in the Slice 7 eval, promote the deferred per-sentence similarity check from "rejected" to a real feature.
- If a provider's streaming chunks split citation tokens across deltas often enough to matter, parse on the accumulated buffer only (already the case) and confirm no token is parsed mid-stream.

## Iteration log

- **2026-05-30 — one-shot example added.** First live run with Ollama `qwen2.5-coder:3b-instruct` produced a competent answer with **zero** `[chunk:cN]` tokens — the model ignored rule 1 entirely. Hit the re-eval trigger above ("revisit the prompt before changing the ID scheme"). Added a single illustrative answer-format example to the system prompt (placeholders `c1`/`c2` with `[chunk:none]`) — small instruction-tuned models follow concrete patterns far better than abstract rules. Lives in `app/citations/prompt.py`; one new unit test in `tests/test_citations.py` asserts the example marker is in the rendered system message so future prompt edits can't silently drop it.
