# ADR 0003: Vendored tiny git repo as ingestion test fixture

**Status**: Accepted
**Date**: 2026-05-25

## Context

The ingestion service shells out to `git clone`. We need to test the full ingestion path (clone → walk → filter → DB upsert) deterministically. Three options were considered:

1. **Vendor a tiny git repo** under `backend/tests/fixtures/sample-repo/`. Tests clone it via a `file://` URL.
2. **Mock the `subprocess` call** to `git`. Tests bypass the clone and work from a pre-laid-out directory.
3. **Clone a real public repo** (e.g. `octocat/Hello-World`) in tests.

## Decision

Vendor a tiny git repository (≈5–10 files, 1–2 commits) under `backend/tests/fixtures/sample-repo/`. Tests clone it via a `file://` URL into a tmpdir.

To avoid git's submodule-boundary semantics for nested `.git/` directories (a checked-in `.git/` inside the working tree is treated as a gitlink, not as tracked content), the fixture's git metadata is stored on disk as `dot-git/`. A small test helper copies `dot-git/` → `.git/` into the tmpdir before cloning.

The fixture intentionally includes one of each junk type that the ingestion filter (ADR 0004) is supposed to skip — a lockfile, a binary file, a `node_modules`-style directory — so tests can assert filter behavior alongside happy-path indexing.

## Consequences

**Upside**: Hermetic — no network, no rate limits, reproducible across machines. Exercises the real `git clone` path and the real filesystem walk; the integration most likely to break is the one being tested. One fixture serves both happy-path and filter-assertion tests, which keeps the suite focused.

**Cost**: The `dot-git → .git` rename is mildly cute and needs a comment in the fixture helper so a reader doesn't trip over it. Committing what is effectively a git repo inside a git repo also looks odd at a glance; a `README` inside the fixture directory mitigates that.

**Rejected alternatives**:
- *Mocking `subprocess.run`*: skips the exact integration we most need to test (subprocess + filesystem walk + DB upsert all together). Mock tests that pass when the real pipeline is broken are worse than no test.
- *Cloning a real public repo*: flaky (network), slow (clone time), and rate-limit risk in CI.
