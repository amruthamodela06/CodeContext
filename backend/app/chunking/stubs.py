"""Stub chunkers for languages whose implementations land in a later slice.

Each subclass raises NotImplementedError with the rule sketch in the docstring,
so the chunker_for() registry is complete from day one and switching one on is
a localized change.
"""

from app.chunking.protocol import Chunk, Chunker


class TypeScriptChunker(Chunker):
    """Rules (sketch, not implemented):

    - Top-level `function` declarations and `const = () => {...}` → `function`.
    - `class` → `class`; each method inside → `method` (`parent_name` = class).
    - `interface` declarations → `interface_decl`.
    - `type X = ...` aliases → `type_alias`.
    - Top of file (imports + constants) → `module_preamble`.
    - `extra_metadata`: `{"exports": [...], "is_component": bool}`
      (React heuristic: function-returning-JSX or `extends React.Component`).
    """

    language = "TypeScript"

    def chunk(self, source: str) -> list[Chunk]:
        raise NotImplementedError("TypeScript chunking is a Slice 2.5+ task")


class JavaScriptChunker(Chunker):
    """Rules (sketch): same as TypeScript minus `interface_decl` and
    `type_alias`. JavaScript-specific bits land in `extra_metadata.exports`.
    """

    language = "JavaScript"

    def chunk(self, source: str) -> list[Chunk]:
        raise NotImplementedError("JavaScript chunking is a Slice 2.5+ task")


class GoChunker(Chunker):
    """Rules (sketch, not implemented):

    - Top-level `func` → `function`.
    - `func` with a receiver → `method`; `parent_name` = receiver type
      (pointer-stripped).
    - `type X struct` → `struct_decl`; `type X interface` → `interface_decl`.
    - Package doc comment → `module_docstring`.
    - `import` / `const` / `var` blocks → `module_preamble`.
    - `extra_metadata`: `{"receiver_pointer": bool, "exported": bool}`.
    """

    language = "Go"

    def chunk(self, source: str) -> list[Chunk]:
        raise NotImplementedError("Go chunking is a Slice 2.5+ task")


class RustChunker(Chunker):
    """Rules (sketch, not implemented):

    - Top-level `fn` → `function`.
    - `fn` inside an `impl` block → `method`; `parent_name` = impl target.
    - `struct` / `enum` / `trait` → `struct_decl` / `enum_decl` / `trait_decl`.
    - `use` + top-level `const`/`static` → `module_preamble`.
    - `macro_rules!` → `macro_def`.
    - `extra_metadata`: `{"trait_impl": str | None, "visibility": str | None}`.
    """

    language = "Rust"

    def chunk(self, source: str) -> list[Chunk]:
        raise NotImplementedError("Rust chunking is a Slice 2.5+ task")
