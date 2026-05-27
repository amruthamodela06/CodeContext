"""PythonChunker — tree-sitter-driven AST chunking for Python source.

Rules are defined in ADR 0008. The short version:
- Top-level `string` (or `expression_statement[string]`) at the start of a
  module is the module docstring.
- Contiguous module-level statements between the docstring and the first
  function/class become the `module_preamble`.
- Top-level `function_definition` (incl. `async def`) → `function`.
- Top-level `class_definition` → `class` (signature + class-level body up to
  the first method); each method inside → `method`.
- `decorated_definition` wrappers around either of the above are unwrapped;
  the chunk's start_line is the first decorator's row.
- Nested functions stay inside their parent's content; they are not emitted
  as separate chunks.
- Parse failures return [] and log; ingestion is never crashed by chunking.
"""

from __future__ import annotations

import logging
from typing import Any

from tree_sitter import Node
from tree_sitter_language_pack import get_parser

from app.chunking.protocol import Chunk, Chunker

log = logging.getLogger(__name__)

_PARSER = get_parser("python")

# Decorator names whose presence we surface as boolean metadata for cheap
# filtering (callers don't have to JSONB-introspect the decorators list).
_FLAGGED_DECORATORS = ("classmethod", "staticmethod", "property")


class PythonChunker(Chunker):
    language = "Python"

    def chunk(self, source: str) -> list[Chunk]:
        # Normalize line endings (ADR 0008) — windows clones occasionally
        # produce CRLF in tempdirs even with the project's LF policy.
        source = source.replace("\r\n", "\n").replace("\r", "\n")
        # Tree-sitter's start_byte/end_byte are UTF-8 byte offsets, not str
        # code-point offsets. Slice the encoded source so multi-byte
        # characters (em-dashes, accented identifiers, ...) don't shift the
        # chunk content.
        source_bytes = source.encode("utf-8")

        try:
            tree = _PARSER.parse(source)
        except Exception as exc:
            log.warning("tree-sitter parse raised: %s", exc)
            return []

        root = tree.root_node()
        if root.has_error():
            # Tree-sitter recovered enough to give us a tree; log and continue.
            log.warning("python parse produced ERROR nodes; chunking partial result")

        top: list[Node] = [root.child(i) for i in range(root.child_count())]
        chunks: list[Chunk] = []

        # 1. Module docstring (first child only).
        docstring_end_idx = -1
        if top and _is_module_docstring(top[0]):
            chunks.append(self._simple_chunk(top[0], source_bytes, "module_docstring"))
            docstring_end_idx = 0

        # 2. Module preamble = everything from after the docstring up to the
        #    first def/class. Boundary tightened per the Plan-agent's review.
        body_start_idx = len(top)
        for idx in range(docstring_end_idx + 1, len(top)):
            if _is_def_or_class(top[idx]):
                body_start_idx = idx
                break
        preamble = top[docstring_end_idx + 1 : body_start_idx]
        preamble_chunk = self._preamble_chunk(preamble, source_bytes)
        if preamble_chunk is not None:
            chunks.append(preamble_chunk)

        # 3. Body: defs, classes, top_level_blocks (in source order).
        for node in top[body_start_idx:]:
            inner, decorators = _unwrap_decorated(node)
            kind = inner.kind() if inner is not None else node.kind()

            if kind == "function_definition" and inner is not None:
                chunks.append(
                    self._function_chunk(
                        node,
                        inner,
                        decorators,
                        source_bytes,
                        chunk_type="function",
                        parent_name=None,
                    )
                )
            elif kind == "class_definition" and inner is not None:
                chunks.extend(self._class_and_methods(node, inner, decorators, source_bytes))
            else:
                block_chunk = self._top_level_block(node, source_bytes)
                if block_chunk is not None:
                    chunks.append(block_chunk)

        return chunks

    # --- chunk builders ---------------------------------------------------

    def _simple_chunk(self, node: Node, source_bytes: bytes, chunk_type: Any) -> Chunk:
        return Chunk(
            chunk_type=chunk_type,
            name=None,
            parent_name=None,
            start_line=node.start_position().row + 1,
            end_line=node.end_position().row + 1,
            content=_slice(source_bytes, node.start_byte(), node.end_byte()),
            language=self.language,
        )

    def _preamble_chunk(self, nodes: list[Node], source_bytes: bytes) -> Chunk | None:
        if not nodes:
            return None
        first, last = nodes[0], nodes[-1]
        return Chunk(
            chunk_type="module_preamble",
            name=None,
            parent_name=None,
            start_line=first.start_position().row + 1,
            end_line=last.end_position().row + 1,
            content=_slice(source_bytes, first.start_byte(), last.end_byte()),
            language=self.language,
        )

    def _function_chunk(
        self,
        outer: Node,
        inner: Node,
        decorators: list[Node],
        source_bytes: bytes,
        *,
        chunk_type: Any,
        parent_name: str | None,
    ) -> Chunk:
        name = _text_of(inner.child_by_field_name("name"), source_bytes)
        return Chunk(
            chunk_type=chunk_type,
            name=name,
            parent_name=parent_name,
            start_line=outer.start_position().row + 1,
            end_line=outer.end_position().row + 1,
            content=_slice(source_bytes, outer.start_byte(), outer.end_byte()),
            language=self.language,
            is_async=_is_async_function(inner),
            extra_metadata=_decorator_metadata(decorators, source_bytes),
        )

    def _class_and_methods(
        self,
        outer: Node,
        inner: Node,
        decorators: list[Node],
        source_bytes: bytes,
    ) -> list[Chunk]:
        """Emit the class chunk + one chunk per method.

        The class chunk's content runs from the start of the (possibly
        decorated) class to the line just before the first method. If the
        class has no methods, the chunk covers the whole class.
        """
        class_name = _text_of(inner.child_by_field_name("name"), source_bytes)
        body = inner.child_by_field_name("body")

        first_method_idx: int | None = None
        if body is not None:
            for i in range(body.child_count()):
                child = body.child(i)
                child_inner, _ = _unwrap_decorated(child)
                if child_inner is not None and child_inner.kind() == "function_definition":
                    first_method_idx = i
                    break

        if first_method_idx is None or body is None:
            # No methods — class chunk is the whole class.
            class_chunk = Chunk(
                chunk_type="class",
                name=class_name,
                parent_name=None,
                start_line=outer.start_position().row + 1,
                end_line=outer.end_position().row + 1,
                content=_slice(source_bytes, outer.start_byte(), outer.end_byte()),
                language=self.language,
                extra_metadata=_decorator_metadata(decorators, source_bytes),
            )
            return [class_chunk]

        first_method = body.child(first_method_idx)
        # end_line of the class chunk = the line immediately before the
        # first method's first row. `start_position().row` is 0-indexed; when
        # treated as a 1-indexed line number it equals (method_line - 1).
        class_end_line = first_method.start_position().row
        # Ensure start <= end even for pathological cases (e.g. one-line class).
        class_end_line = max(class_end_line, outer.start_position().row + 1)
        class_content = _slice(source_bytes, outer.start_byte(), first_method.start_byte()).rstrip()

        chunks: list[Chunk] = [
            Chunk(
                chunk_type="class",
                name=class_name,
                parent_name=None,
                start_line=outer.start_position().row + 1,
                end_line=class_end_line,
                content=class_content,
                language=self.language,
                extra_metadata=_decorator_metadata(decorators, source_bytes),
            )
        ]

        for i in range(first_method_idx, body.child_count()):
            method_outer = body.child(i)
            method_inner, method_decorators = _unwrap_decorated(method_outer)
            if method_inner is not None and method_inner.kind() == "function_definition":
                chunks.append(
                    self._function_chunk(
                        method_outer,
                        method_inner,
                        method_decorators,
                        source_bytes,
                        chunk_type="method",
                        parent_name=class_name,
                    )
                )
            # Non-method body items (e.g. nested class, more class-level
            # statements) after the first method are intentionally folded
            # into the preceding method chunk's gap and not emitted; class
            # body shapes that lean on this are uncommon.

        return chunks

    def _top_level_block(self, node: Node, source_bytes: bytes) -> Chunk | None:
        content = _slice(source_bytes, node.start_byte(), node.end_byte())
        stripped = content.strip()
        if not stripped or stripped == "pass" or stripped == "...":
            return None
        return Chunk(
            chunk_type="top_level_block",
            name=None,
            parent_name=None,
            start_line=node.start_position().row + 1,
            end_line=node.end_position().row + 1,
            content=content,
            language=self.language,
        )


# --- helpers ---------------------------------------------------------------


def _is_module_docstring(node: Node) -> bool:
    """Top-level docstring in tree-sitter-python: bare `string`, or
    `expression_statement` wrapping a `string` (depending on grammar version).
    """
    if node.kind() == "string":
        return True
    if node.kind() == "expression_statement" and node.child_count() > 0:
        return node.child(0).kind() == "string"
    return False


def _is_def_or_class(node: Node) -> bool:
    if node.kind() in ("function_definition", "class_definition"):
        return True
    if node.kind() == "decorated_definition":
        inner, _ = _unwrap_decorated(node)
        return inner is not None and inner.kind() in (
            "function_definition",
            "class_definition",
        )
    return False


def _unwrap_decorated(node: Node) -> tuple[Node | None, list[Node]]:
    """If node is a `decorated_definition`, return (inner_def, [decorators]).
    Otherwise return (node, []).
    """
    if node.kind() != "decorated_definition":
        return node, []
    decorators: list[Node] = []
    inner: Node | None = None
    for i in range(node.child_count()):
        c = node.child(i)
        if c.kind() == "decorator":
            decorators.append(c)
        elif c.kind() in ("function_definition", "class_definition"):
            inner = c
    return inner, decorators


def _is_async_function(fn: Node) -> bool:
    """`async def` shows up as a function_definition with an unnamed `async`
    keyword child (first non-named token).
    """
    return any(fn.child(i).kind() == "async" for i in range(fn.child_count()))


def _decorator_metadata(decorators: list[Node], source_bytes: bytes) -> dict[str, Any]:
    """Return the metadata dict for a (possibly empty) decorator list."""
    texts: list[str] = []
    for d in decorators:
        raw = _slice(source_bytes, d.start_byte(), d.end_byte())
        # Strip leading "@" and trailing whitespace; tolerate `@  name`.
        texts.append(raw.lstrip("@").strip())

    metadata: dict[str, Any] = {"decorators": texts} if texts else {}
    for text in texts:
        base = text.split("(", 1)[0].strip()
        if base in _FLAGGED_DECORATORS:
            metadata[f"is_{base}"] = True
    return metadata


def _text_of(node: Node | None, source_bytes: bytes) -> str | None:
    if node is None:
        return None
    return _slice(source_bytes, node.start_byte(), node.end_byte())


def _slice(source_bytes: bytes, start: int, end: int) -> str:
    """Slice the UTF-8-encoded source between two tree-sitter byte offsets."""
    return source_bytes[start:end].decode("utf-8", errors="replace")
