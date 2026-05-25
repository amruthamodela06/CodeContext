# ADR 0005: Git client = `subprocess` to the system `git` binary

**Status**: Accepted
**Date**: 2026-05-25

## Context

Slice 1's ingestion service needs to clone a public GitHub repo and walk its filesystem. That's the entire git surface area. Future slices that touch commit history will pull PR and issue data from the GitHub REST/GraphQL APIs — not local `.git` — because that data only exists on GitHub.

Three options considered:

1. **`subprocess`** to the system `git` binary.
2. **`gitpython`** — pure-Python wrapper around the same `git` CLI.
3. **`pygit2`** — Python bindings over `libgit2` (C library, faster, no `git` binary required).

## Decision

Use `subprocess` to call the system `git` binary. Specifically: `git clone --depth 1 <url> <dest>`, then a stdlib filesystem walk (`pathlib.Path.rglob` or `os.walk`). No third-party git library.

## Consequences

**Upside**: Zero new dependencies. `subprocess`, `pathlib`, and `os` are stdlib. The operations we need (clone + walk) are two calls each in a handful of lines.

**Cost**: Requires `git` to be on PATH at runtime. README will document this. The eventual deployed container (Railway/Fly.io) will need `git` installed; this is one `apt-get` line in the Dockerfile.

**Rejected alternatives**:
- *gitpython*: wraps the same `git` CLI we'd call ourselves; adds a dependency without removing the runtime requirement on the `git` binary. Its real value is in commit/diff/index manipulation that Slice 1 doesn't need.
- *pygit2*: faster and removes the `git` binary requirement, but C bindings have known wheel-availability friction on Windows; we don't need its speed and don't want its install friction.

**Re-evaluation trigger**: if/when a future slice needs richer local-git operations (walking the commit graph, computing diffs between commits, `git blame`), revisit via ADR 0005-amendment-A. The GitHub API likely still wins for most of those cases because we want PR/issue context bound to commits, but a faster local path is worth measuring at that point.
