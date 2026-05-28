# ADR 0007: Open-source embeddings + provider-abstracted LLM with Gemini default

**Status**: Accepted (default embedding *variant* amended by ADR 0009)
**Date**: 2026-05-26

> **Amendment (2026-05-27, ADR 0009):** this ADR named the embedding default as `bge-base-en-v1.5`. Slice 3 flips the default *variant* to `bge-small-en-v1.5` (384-dim) for ~2-3× faster CPU embedding; bge-base becomes the ablation comparison. The provider abstraction and the `bge` family choice below are unchanged.

## Context

PRD §9 originally specified OpenAI `text-embedding-3-small` for embeddings and GPT-4o-mini as the deployed LLM. Two pressures pushed for a revision before Slice 2 starts:

1. **Cost discipline for a public demo.** A portfolio project that demos publicly should run on free tiers by default. Requiring an OpenAI API key — or eating cost out of pocket — creates friction for hiring reviewers who want to click the live demo and try it.
2. **Dev-laptop constraint.** Primary dev machine is a Windows 11 AMD Ryzen 5 7535HS with integrated Radeon 660M graphics and 16 GB RAM. No discrete GPU; local inference is CPU-only. Whatever embedding model becomes the default has to run on CPU at usable throughput, *or* we depend entirely on a remote API.

Provider abstraction was also weighing: PRD §8 calls eval a first-class deliverable with ablation studies across embedding and LLM choices. Hard-coding one provider per layer pushes the ablation harness into ugly conditional code.

## Decision

### Embeddings

Use **`bge-base-en-v1.5`** via `sentence-transformers`, running in-process in the Python backend on CPU. Wrap behind an `Embedder` interface so the implementation can be swapped for ablation or by configuration:

- `bge-base-en-v1.5` — default; local; CPU
- OpenAI `text-embedding-3-small` — API; paid
- Voyage `voyage-code-2` — API; code-specialized; paid
- Ollama `nomic-embed-text` — local; CPU; alternative OSS option

### LLM

Use **Gemini 2.0 Flash via the free tier** as the deployed default. Wrap behind an `LLMProvider` interface with four implementations:

- `gemini` — default; free tier
- `openai` — GPT-4o-mini; paid; ablation
- `anthropic` — Claude Haiku/Sonnet; paid; ablation
- `ollama` — local Qwen 2.5 Coder 3B Instruct; CPU; offline-dev and ablation

Provider chosen via the `LLM_PROVIDER` env variable. **All providers must support streaming** (server-sent events to the UI). Accessed via the OpenAI-compatible SDK pattern — each provider's compatibility shim, or a thin adapter — so client code is the same shape across providers.

### Env-variable contract

Both interfaces resolve their concrete implementation from environment at startup:

- `LLM_PROVIDER` — one of `gemini` (default), `openai`, `anthropic`, `ollama`.
- `EMBEDDING_PROVIDER` — one of `bge` (default), `openai`, `voyage`, `ollama`.

Per-provider credentials live in their own env vars (`GEMINI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `VOYAGE_API_KEY`, `OLLAMA_BASE_URL`). Loaded via `pydantic-settings`; only the credentials matching the selected provider are validated as required at startup.

## Consequences

**Upside**

- Zero recurring cost for the default deployed path. PRD §7 cost ceiling collapses to "small Railway/Fly infra cost". Easier to publish a public demo without API-key shenanigans.
- Ablation studies fall naturally out of the architecture: set `LLM_PROVIDER=openai`, rerun eval. Same for `Embedder`. The "first-class evaluation" stance from PRD §8 made concrete.
- Local development works fully offline via Ollama. No internet required to iterate on prompts or retrieval tuning.
- No API key required to clone-and-run from a fresh checkout — Gemini free tier + local embeddings.

**Costs**

- More code surface: provider adapters, an `Embedder` interface, factory wiring, env-driven selection. Roughly 150–250 LOC of glue we wouldn't have with one hard-coded provider.
- CPU embeddings are slower than API embeddings. Need to measure batch throughput on the dev laptop early; if it's catastrophic for the 200 k LOC ingestion ceiling (PRD §6.1), fall back to an API embedder for the default path.
- OpenAI-compatible SDK pattern depends on each provider's compatibility-shim quality. Streaming behavior may differ subtly across providers (delimiter, error semantics, finish-reason mapping). Each provider needs a small harness test.
- Gemini free tier has rate and usage limits we'll share across all anonymous users on the shared deployment key. PRD §6.6 rewrite addresses this.
- `bge-base-en-v1.5` is general-purpose, not code-specialized. May underperform `voyage-code-2` on code-heavy retrieval. Acceptable tradeoff for the v1 default; the ablation will quantify the gap.

**Rejected alternatives**

- *Stay with OpenAI + GPT-4o-mini (PRD original).* Highest quality default but locks the demo behind paid API keys. Each reviewer would need to bring their own — breaks the "click the demo URL" flow.
- *Voyage `voyage-code-2` for embeddings as the default.* Genuinely better on code; paid-only, no free tier. Saved for the BYO-config tier and ablation.
- *Self-hosted larger LLM (e.g., DeepSeek-Coder-V2 7B) for the default.* Overkill for CPU inference on a 16 GB integrated-graphics laptop. Qwen 2.5 Coder 3B Instruct is the largest local model that runs at usable speed on the dev box; even that is the offline-dev path, not the production default.

**Re-evaluation triggers**

- If `bge-base-en-v1.5` embedding throughput on CPU can't index a 50 k LOC repo in under ~2 minutes, switch the default to an API embedder.
- If the shared Gemini free-tier quota becomes a bottleneck (anonymous users hitting rate limits regularly), revisit the cost model — cap anonymous tier harder, or switch to a paid model as the default and require sign-in.
- If a provider's OpenAI-compatible shim breaks streaming in a way we can't easily work around, that provider drops to "not supported" rather than us building a bespoke streaming client.
