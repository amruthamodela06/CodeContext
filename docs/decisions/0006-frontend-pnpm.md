# ADR 0006: Frontend package manager = `pnpm`

**Status**: Accepted
**Date**: 2026-05-25

## Context

The frontend will be scaffolded in Checkpoint E with `pnpm create next-app`. Before that, we need to commit to a Node package manager: `pnpm`, `npm`, or `yarn`. Locking it now avoids re-litigating it when the scaffold runs.

## Decision

Use **`pnpm`**.

## Consequences

**Upside**:
- Fastest installs of the three (content-addressable global store; packages are hard-linked into `node_modules` rather than re-copied per project).
- Strict by default — refuses to resolve "phantom" dependencies (packages you `import` but don't declare in `package.json`). Catches a class of bugs before they ship.
- Deterministic, human-readable `pnpm-lock.yaml`.
- First-class support in Next.js docs and `create-next-app` templates.

**Cost**: Requires a global install. Less universal than `npm` (which ships with Node). CI configurations need an explicit `pnpm/action-setup` step.

**Install path** (deferred to Checkpoint E): either `npm install -g pnpm`, or — preferred — `corepack enable && corepack prepare pnpm@latest --activate`, which lets Node manage the pnpm version per project via the `packageManager` field in `package.json`. README will document.

**Rejected alternatives**:
- *npm*: fine, ships with Node, but slower and looser. Phantom-dependency bugs go unnoticed until production.
- *yarn classic (v1)*: effectively unmaintained.
- *yarn berry (v2+) with PnP*: introduces its own ecosystem (no real `node_modules`); great in theory but a long tail of tool-compatibility issues with bundlers, IDEs, and test runners.

**Re-evaluation trigger**: if pnpm's strict `node-linker=isolated` semantics break a third-party tool we need (a Storybook plugin, certain bundlers, certain test runners) and the workaround is uglier than just switching, open ADR 0006-amendment-A.
