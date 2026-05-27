# ADR 0008: tree-sitter integration + Python chunking rules

**Status**: Accepted
**Date**: 2026-05-27

## Context

Slice 2 introduces AST-aware chunking of source files. We need:

1. A Python binding for tree-sitter that ships pre-built wheels for our four target languages (Python, TypeScript/JavaScript, Go, Rust) on Windows and Linux.
2. A concrete chunking policy per language. Slice 2 only implements Python; the other three get interface stubs.
3. A defensible answer to "what does a chunk *mean*?" — line ranges, content normalization, error recovery.

The dev box is Windows 11 / AMD Ryzen 5 7535HS / no GPU / 16 GB RAM / Python 3.14 via uv. tree-sitter historically had rough Windows edges; we want to know early if we're walking into one.

## Decision

### Package

Use **`tree-sitter-language-pack`** (PyPI; latest 1.8.1 at decision time). Single dependency that bundles grammars for all four future languages plus many more. Ships pre-built wheels using the **CPython stable ABI** (`cp310-abi3`), which means a single wheel works on Python 3.10 → current. The Python 3.14 risk flagged during planning **did not materialize** — verified via PyPI's JSON API before adding the dep:

```
tree_sitter_language_pack-1.8.1-cp310-abi3-win_amd64.whl
tree_sitter_language_pack-1.8.1-cp310-abi3-manylinux_2_34_x86_64.whl
tree_sitter_language_pack-1.8.1-cp310-abi3-macosx_*
```

No native build, no wheel-version pinning, no Windows-specific friction.

### Fallback ladder (not exercised; recorded for future-us)

If language-pack ever loses Windows ABI3 coverage:

1. Drop to `tree-sitter` (core) + `tree-sitter-python` for the slice's actual needs. Wider wheel coverage at the cost of one dep per future language.
2. Pin Python in `requires-python` to a version with wheels.
3. Build grammars from C source — last resort, brings a C toolchain dep to Windows.

WSL2 is explicitly **not** in the ladder: it doesn't solve a Python-version problem and introduces a dev-env discontinuity.

### Parser cache

`tree-sitter-language-pack` exposes `get_language(name)` and `get_parser(name)`. Both internally cache. No additional caching layer in our code.

### Encoding + line endings

- Files are read as UTF-8 with `errors="replace"`. Weird encodings degrade to best-effort rather than crashing ingestion.
- Tree-sitter accepts `bytes`; we encode after read.
- Chunk `content` is normalized to `\n` line endings before storage so chunk text is reproducible across Windows and Linux clones (matches our `.gitattributes` LF policy).

### Line numbering

Tree-sitter exposes `node.start_point.row` and `node.end_point.row` as **0-indexed**. Our DB columns `start_line` / `end_line` are **1-indexed and inclusive** (matching how citations are presented to users — "line 42" means the 42nd line). Conversion happens at the chunker boundary: `start_line = node.start_point.row + 1`. This is the single source-of-truth for the off-by-one; downstream code never re-converts.

### Parse failures

Tree-sitter has built-in error recovery: a syntactically invalid file produces a tree with `ERROR` nodes. We:

1. Catch parse exceptions at the chunker entry point.
2. Detect `root.has_error == True` and log a `warning` with the file path.
3. Return an **empty chunk list** for that file. Ingestion continues for other files. A persistent symptom (e.g. a frequently-failing file) shows up as zero-chunks in the per-file count visible in the frontend.

## Python chunking rules (implemented this slice)

A *chunk* is a semantically meaningful unit of source. For Python:

| `chunk_type` | What it covers | `name` | `parent_name` |
|---|---|---|---|
| `module_docstring` | First string-literal expression in a `module` node | None | None |
| `module_preamble` | Lines 1 (or post-docstring) through first `def`/`class` − 1: imports, `__all__`, `TYPE_CHECKING`, module-level assignments | None | None |
| `function` | Top-level `function_definition` (incl. `async def`) | Function name | None |
| `class` | `class_definition` body **up to** but not including its first method: signature + docstring + class-level type aliases / `TypeVar` / `__slots__` | Class name | None |
| `method` | `function_definition` directly inside a class body (incl. `async def`) | Method name | Enclosing class name |
| `top_level_block` | Non-trivial module-level statement(s) between two functions/classes, or after the last one (e.g. `if __name__ == "__main__":` guard) | None | None |

**Boundary rules**

- **Decorators stay attached** to the function/method/class they decorate. `start_line` = the first decorator's line.
- **Nested functions and closures** are NOT chunked separately. Their source is captured inside the enclosing `function` or `method` chunk's `content`. Rationale: CodeContext's value is *why*-question retrieval, which usually benefits from surrounding context; one-off inner helpers as their own retrievable units is anti-signal. Re-evaluate if eval shows that nested defs are common retrieval targets.
- **Async functions and methods** share `chunk_type` with their sync counterparts. The boolean is captured in the column `is_async`, not in `extra_metadata` — async-vs-sync is the most likely metadata filter and deserves a queryable column.
- **Trivial blocks** (whitespace only, single-line `pass`, single bare `...`) at the top level are dropped — they aren't meaningful retrieval targets.

**`extra_metadata` (Python)**

JSONB column. Free-form per language. For Python:

```json
{
  "decorators": ["staticmethod", "lru_cache(maxsize=128)", "app.route(\"/\")"],
  "is_classmethod": false,
  "is_staticmethod": false,
  "is_property": false
}
```

Each decorator captured as its source string verbatim. Boolean flags are present for the three decorators that meaningfully change semantics (classmethod / staticmethod / property) so we can filter without parsing JSONB strings.

## Chunking rules (stubs, not implemented this slice)

The stubs live in `app/chunking/stubs.py`. Each raises `NotImplementedError("language X chunking is Slice 2.5+")` with the rule sketch in the docstring. Sketches:

- **TypeScript / JavaScript**: top-level `function` declarations and `const = () => {}` → `function`; `class` + each method → `class` + `method`; `interface` and `type` aliases → own chunk types (`interface_decl`, `type_alias`); top of file (imports + constants) → `module_preamble`. `extra_metadata`: `{"exports": [...], "is_component": bool}`.
- **Go**: top-level `func` → `function`; `func` with a receiver → `method` with `parent_name` = receiver type (pointer stripped); `type X struct` → `struct_decl`, `type X interface` → `interface_decl`; package doc comment → `module_docstring`; `import`/`const`/`var` blocks → `module_preamble`. `extra_metadata`: `{"receiver_pointer": bool, "exported": bool}`.
- **Rust**: top-level `fn` → `function`; `fn` inside an `impl` block → `method` with `parent_name` = impl target (optionally with trait); `struct`/`enum`/`trait` → `struct_decl`/`enum_decl`/`trait_decl`; `use` + top-level `const`/`static` → `module_preamble`; `macro_rules!` → `macro_def`. `extra_metadata`: `{"trait_impl": "Display" | null, "visibility": "pub" | "pub(crate)" | null}`.

## Integration with /ingest

`ingest_repo` invokes `chunk_repo(repo, session)` after `await session.commit()`, wrapped in a `try/except`. Chunking failures log a warning and **do not** fail ingest. The dedicated `POST /repos/{repo_id}/chunk` endpoint stays useful as a retry path. The `/ingest` response shape is unchanged — chunks are not embedded in the response (additive change only).

No background job queue (RQ / arq) this slice. Chunking is sync; revisit if measurements show > ~3 s on a 200k LOC repo.

## Consequences

**Upside**

- One package covers all four target languages (no per-language wheel churn as we expand).
- Pre-built ABI3 wheels mean zero compile-on-install friction on Windows, Linux, and macOS.
- Chunking is CPU-only, stays inside the GPU-free constraint locked in ADR 0007 and the dev-env note in PRD §9.
- Stub interface for the other three languages means Slice 2.5 (or wherever we add TS/JS/Go/Rust chunking) is a localized change.

**Costs**

- `tree-sitter-language-pack` is a moderately large dep (~30 MB once installed; ships pre-built grammar shared libraries for ~100 languages). We only use four. Acceptable for a self-contained portfolio app; revisit if cold-start times become a Railway/Fly issue.
- The `cp310-abi3` wheel uses the slower CPython stable ABI rather than the version-specific ABI. Minor perf delta (~5–10%) on parsing throughput; negligible for our corpus sizes.
- Nested functions are not separately chunked. If eval reveals these are common retrieval targets, we change the rule and re-chunk (the upsert pattern via cascade makes re-chunking cheap).

**Re-evaluation triggers**

- `tree-sitter-language-pack` parsing throughput on Windows CPU is < 500 files/sec → switch to per-language packages or move parsing to a background worker.
- A future grammar update breaks ABI3 compatibility → pin the language-pack version and document.
- Eval shows that 30%+ of useful retrieval hits live in nested functions → flip the nested-fn rule from "fold into parent" to "emit separately".
