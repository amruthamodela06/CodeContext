"""Unit tests for app.chunking.python.PythonChunker.

Pure tests against fixtures in tests/fixtures/python_chunk_samples/. No DB,
no network, no tree-sitter mocking — the real parser runs against real
source files.
"""

from pathlib import Path

import pytest

from app.chunking import (
    Chunk,
    GoChunker,
    JavaScriptChunker,
    PythonChunker,
    RustChunker,
    TypeScriptChunker,
    chunker_for,
)

FIXTURES = Path(__file__).parent / "fixtures" / "python_chunk_samples"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _by_name(chunks: list[Chunk], name: str) -> Chunk:
    matches = [c for c in chunks if c.name == name]
    assert len(matches) == 1, f"expected exactly one chunk named {name!r}, got {len(matches)}"
    return matches[0]


# --- chunker_for factory --------------------------------------------------


def test_chunker_for_python_returns_pythonchunker() -> None:
    chunker = chunker_for("Python")
    assert isinstance(chunker, PythonChunker)


def test_chunker_for_unsupported_returns_none() -> None:
    assert chunker_for(None) is None
    assert chunker_for("") is None
    assert chunker_for("Haskell") is None


def test_stub_chunkers_raise_not_implemented() -> None:
    for cls in (TypeScriptChunker, JavaScriptChunker, GoChunker, RustChunker):
        with pytest.raises(NotImplementedError):
            cls().chunk("anything")


# --- Comprehensive happy path --------------------------------------------


def test_comprehensive_emits_all_chunk_types() -> None:
    source = _load("comprehensive.py")
    chunks = PythonChunker().chunk(source)
    types = [c.chunk_type for c in chunks]
    assert "module_docstring" in types
    assert "module_preamble" in types
    assert types.count("function") == 2  # cached_thing, fetch
    assert types.count("class") == 1
    assert types.count("method") == 5  # __init__, make_default, from_dict, doubled, render_async
    assert types.count("top_level_block") == 1  # if __name__ == "__main__":


def test_comprehensive_module_docstring_content() -> None:
    chunks = PythonChunker().chunk(_load("comprehensive.py"))
    docstring = next(c for c in chunks if c.chunk_type == "module_docstring")
    assert docstring.start_line == 1
    assert docstring.name is None
    assert "comprehensive sample" in docstring.content


def test_comprehensive_module_preamble_covers_imports() -> None:
    chunks = PythonChunker().chunk(_load("comprehensive.py"))
    preamble = next(c for c in chunks if c.chunk_type == "module_preamble")
    assert "from __future__ import annotations" in preamble.content
    assert "from functools import lru_cache" in preamble.content
    assert "VERSION" in preamble.content
    # Preamble must NOT cross into the first function.
    assert "def cached_thing" not in preamble.content


def test_comprehensive_decorator_metadata() -> None:
    chunks = PythonChunker().chunk(_load("comprehensive.py"))
    cached = _by_name(chunks, "cached_thing")
    assert cached.chunk_type == "function"
    assert cached.is_async is False
    assert "lru_cache(maxsize=128)" in cached.extra_metadata["decorators"]


def test_comprehensive_async_function() -> None:
    chunks = PythonChunker().chunk(_load("comprehensive.py"))
    fetch = _by_name(chunks, "fetch")
    assert fetch.chunk_type == "function"
    assert fetch.is_async is True


def test_comprehensive_class_chunk_stops_before_first_method() -> None:
    chunks = PythonChunker().chunk(_load("comprehensive.py"))
    widget = _by_name(chunks, "Widget")
    assert widget.chunk_type == "class"
    # Class signature + docstring + DEFAULT_SIZE class attribute should be
    # included; the first method (__init__) should NOT be.
    assert "DEFAULT_SIZE" in widget.content
    assert "def __init__" not in widget.content


def test_comprehensive_methods_have_parent_name() -> None:
    chunks = PythonChunker().chunk(_load("comprehensive.py"))
    init = _by_name(chunks, "__init__")
    assert init.chunk_type == "method"
    assert init.parent_name == "Widget"


def test_comprehensive_staticmethod_flagged() -> None:
    chunks = PythonChunker().chunk(_load("comprehensive.py"))
    md = _by_name(chunks, "make_default")
    assert md.extra_metadata.get("is_staticmethod") is True
    assert "staticmethod" in md.extra_metadata["decorators"]


def test_comprehensive_classmethod_flagged() -> None:
    chunks = PythonChunker().chunk(_load("comprehensive.py"))
    fd = _by_name(chunks, "from_dict")
    assert fd.extra_metadata.get("is_classmethod") is True


def test_comprehensive_property_flagged() -> None:
    chunks = PythonChunker().chunk(_load("comprehensive.py"))
    doubled = _by_name(chunks, "doubled")
    assert doubled.extra_metadata.get("is_property") is True


def test_comprehensive_async_method() -> None:
    chunks = PythonChunker().chunk(_load("comprehensive.py"))
    render = _by_name(chunks, "render_async")
    assert render.chunk_type == "method"
    assert render.is_async is True
    assert render.parent_name == "Widget"


def test_comprehensive_top_level_block() -> None:
    chunks = PythonChunker().chunk(_load("comprehensive.py"))
    block = next(c for c in chunks if c.chunk_type == "top_level_block")
    assert "__main__" in block.content
    assert block.name is None


def test_comprehensive_line_numbers_are_1_indexed_inclusive() -> None:
    """End-to-end check that line numbers map back to readable source."""
    source = _load("comprehensive.py")
    lines = source.split("\n")
    chunks = PythonChunker().chunk(source)
    # First chunk (docstring) starts at line 1.
    docstring = next(c for c in chunks if c.chunk_type == "module_docstring")
    assert lines[docstring.start_line - 1].startswith('"""')
    # The class chunk's start_line should point at "class Widget:"
    widget = _by_name(chunks, "Widget")
    assert "class Widget" in lines[widget.start_line - 1]


# --- Nested functions are folded -----------------------------------------


def test_nested_functions_are_folded_into_parent() -> None:
    chunks = PythonChunker().chunk(_load("nested_functions.py"))
    names = sorted(c.name for c in chunks if c.name is not None)
    # Only the two outer functions; nested ones are part of parent content.
    assert names == ["another", "outer"]
    # And the inner function source lives inside the parent's content.
    outer = _by_name(chunks, "outer")
    assert "def inner()" in outer.content


# --- File with no module docstring ---------------------------------------


def test_no_docstring_still_emits_preamble_and_function() -> None:
    chunks = PythonChunker().chunk(_load("no_docstring.py"))
    types = [c.chunk_type for c in chunks]
    assert "module_docstring" not in types
    assert "module_preamble" in types
    assert any(c.name == "standalone" for c in chunks)


# --- Parse failure ---------------------------------------------------------


def test_malformed_file_returns_chunks_without_crashing() -> None:
    """Tree-sitter recovers from parse errors gracefully. The chunker must
    either return [] or partial chunks — never raise. ADR 0008's parse-failure
    policy ensures ingestion is never killed by a broken file.
    """
    source = _load("malformed.py")
    chunks = PythonChunker().chunk(source)
    # Don't assert on exact content — tree-sitter's recovery is best-effort
    # and could produce 0 or a few chunks. Just that it didn't raise.
    assert isinstance(chunks, list)


# --- CRLF normalization ---------------------------------------------------


def test_chunker_normalizes_crlf_to_lf() -> None:
    """ADR 0008 requires content to be \\n-normalized."""
    source = "def foo():\r\n    return 1\r\n"
    chunks = PythonChunker().chunk(source)
    foo = _by_name(chunks, "foo")
    assert "\r" not in foo.content
    assert foo.content.count("\n") >= 1
