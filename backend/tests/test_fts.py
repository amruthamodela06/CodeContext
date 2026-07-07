"""Tests for Slice 6b -- FTS content composition (split_identifier /
extract_docstring / compute_chunk_fts / backfill_chunk_fts).

The generated tsvector formula itself is exercised end-to-end in the
6e BM25Retriever tests + the /query pipeline test; this file focuses
on the pure Python preprocessing that feeds it.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.fts import (
    backfill_chunk_fts,
    compute_chunk_fts,
    extract_docstring,
    split_identifier,
)
from app.models import CodeChunk, File, Repo

# --- split_identifier -----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected_tokens"),
    [
        ("", set()),
        ("f", {"f"}),
        # camelCase: original + splits. Case is preserved by the splitter;
        # Postgres's english tokenizer lowercases at index time so `By` and
        # `by` collapse to the same lexeme downstream.
        ("getUserByEmail", {"getUserByEmail", "get", "User", "By", "Email"}),
        # snake_case
        ("parse_html_response", {"parse_html_response", "parse", "html", "response"}),
        # hyphenated
        ("send-http-request", {"send-http-request", "send", "http", "request"}),
        # PascalCase acronym run: HTTPServer -> HTTP + Server
        ("HTTPServer", {"HTTPServer", "HTTP", "Server"}),
        # Two-acronym-then-camel: HTTPSProxy -> HTTPS + Proxy
        ("HTTPSProxy", {"HTTPSProxy", "HTTPS", "Proxy"}),
        # Digits: preserved verbatim, no extra split
        ("v2Client", {"v2Client", "v2", "Client"}),
        # Two-char identifier: splits but subtokens shorter than 2 chars
        # are dropped, so we only get the verbatim form back.
        ("aB", {"aB"}),
    ],
)
def test_split_identifier_produces_expected_tokens(raw: str, expected_tokens: set[str]) -> None:
    result = split_identifier(raw)
    if not raw:
        assert result == ""
        return
    tokens = set(result.split())
    for t in expected_tokens:
        assert t in tokens, f"expected {t!r} in tokens for {raw!r}, got {tokens}"


def test_split_identifier_always_keeps_original_verbatim() -> None:
    """Exact-name search must still hit `getUserByEmail` even after splitting."""
    for raw in ("getUserByEmail", "parse_html", "HTTPServer", "v2Client"):
        assert split_identifier(raw).startswith(raw)


# --- extract_docstring ----------------------------------------------------


def test_extract_docstring_python_triple_double_quote() -> None:
    src = '''def syncify(fn):
    """Run a coroutine synchronously.

    Delegates to the anyio blocking portal.
    """
    return fn
'''
    doc = extract_docstring(src, "Python")
    assert "Run a coroutine synchronously." in doc
    assert "anyio blocking portal" in doc


def test_extract_docstring_python_triple_single_quote() -> None:
    src = "class Foo:\n    '''One-line class doc.'''\n    pass\n"
    assert extract_docstring(src, "Python") == "One-line class doc."


def test_extract_docstring_python_async_def() -> None:
    src = 'async def go():\n    """Async docstring."""\n    ...\n'
    assert extract_docstring(src, "Python") == "Async docstring."


def test_extract_docstring_no_docstring_returns_empty() -> None:
    src = "def bare():\n    return 1\n"
    assert extract_docstring(src, "Python") == ""


def test_extract_docstring_unsupported_language_returns_empty() -> None:
    src = "function foo() {\n  /** JSDoc here */\n  return 1;\n}\n"
    # TS/JS doc extraction not implemented in v1.
    assert extract_docstring(src, "TypeScript") == ""


# --- compute_chunk_fts ----------------------------------------------------


def test_compute_chunk_fts_combines_parent_and_name() -> None:
    src = '''def get_user(user_id):
    """Fetch a user by ID."""
    return None
'''
    name, doc, body = compute_chunk_fts(
        name="get_user",
        parent_name="UserService",
        content=src,
        language="Python",
    )
    tokens = set(name.split())
    # Both parent and name land in fts_name, both original + split forms.
    # Case preserved; the Postgres english tokenizer lowercases downstream.
    assert {"UserService", "User", "Service", "get_user", "get", "user"}.issubset(tokens)
    assert doc == "Fetch a user by ID."
    assert body == src  # body is verbatim content, weight D fallback


def test_compute_chunk_fts_handles_missing_parent() -> None:
    name, doc, body = compute_chunk_fts(
        name="helper",
        parent_name=None,
        content="def helper(): pass\n",
        language="Python",
    )
    assert "helper" in name
    assert doc == ""
    assert body == "def helper(): pass\n"  # body is content verbatim


def test_compute_chunk_fts_missing_name_and_parent() -> None:
    """Module-level chunks (imports, top-level statements) have no name."""
    name, doc, body = compute_chunk_fts(
        name=None,
        parent_name=None,
        content="import os\nx = 1\n",
        language="Python",
    )
    assert name == ""
    assert doc == ""
    assert body == "import os\nx = 1\n"


# --- backfill_chunk_fts ---------------------------------------------------

pytestmark_db = pytest.mark.usefixtures("_clean_db")


@pytest.mark.usefixtures("_clean_db")
async def test_backfill_populates_and_is_idempotent(session: AsyncSession) -> None:
    """Two runs produce identical FTS values; batching walks past 500 rows."""
    repo = Repo(owner="o", name="r", default_branch="main")
    session.add(repo)
    await session.commit()

    f = File(repo_id=repo.id, path="x.py", size_bytes=1, language="Python")
    session.add(f)
    await session.commit()

    # Seed a handful of chunks with empty fts_* fields, matching what
    # historical asyncer chunks looked like at 6a-migrate time.
    chunks = [
        CodeChunk(
            repo_id=repo.id,
            file_id=f.id,
            chunk_type="function",
            name=f"getUser_v{i}",
            parent_name=None,
            start_line=i,
            end_line=i + 2,
            content=f'def getUser_v{i}():\n    """Fetch user v{i}."""\n    return None\n',
            language="Python",
        )
        for i in range(5)
    ]
    session.add_all(chunks)
    await session.commit()

    written = await backfill_chunk_fts(session, repo.id, batch_size=2)  # tiny batches
    assert written == 5

    # Every row now has non-empty fts_name / fts_doc; fts_body echoes content.
    for c in chunks:
        await session.refresh(c)
        assert "getUser" in c.fts_name
        assert "get" in c.fts_name  # camelCase split fired
        assert c.fts_doc.startswith("Fetch user v")
        assert c.fts_body == c.content

    # Idempotent: second run yields the same values.
    snapshot = [(c.fts_name, c.fts_doc, c.fts_body) for c in chunks]
    await backfill_chunk_fts(session, repo.id, batch_size=2)
    for c, prior in zip(chunks, snapshot, strict=True):
        await session.refresh(c)
        assert (c.fts_name, c.fts_doc, c.fts_body) == prior


@pytest.mark.usefixtures("_clean_db")
async def test_backfill_scopes_to_repo(session: AsyncSession) -> None:
    """Other repos' chunks stay untouched."""
    repo_a = Repo(owner="o", name="a", default_branch="main")
    repo_b = Repo(owner="o", name="b", default_branch="main")
    session.add_all([repo_a, repo_b])
    await session.commit()
    fa = File(repo_id=repo_a.id, path="x.py", size_bytes=1, language="Python")
    fb = File(repo_id=repo_b.id, path="y.py", size_bytes=1, language="Python")
    session.add_all([fa, fb])
    await session.commit()

    c_a = CodeChunk(
        repo_id=repo_a.id,
        file_id=fa.id,
        chunk_type="function",
        name="fA",
        start_line=1,
        end_line=1,
        content="def fA(): pass\n",
        language="Python",
    )
    c_b = CodeChunk(
        repo_id=repo_b.id,
        file_id=fb.id,
        chunk_type="function",
        name="fB",
        start_line=1,
        end_line=1,
        content="def fB(): pass\n",
        language="Python",
    )
    session.add_all([c_a, c_b])
    await session.commit()

    await backfill_chunk_fts(session, repo_a.id)
    await session.refresh(c_a)
    await session.refresh(c_b)
    assert "fA" in c_a.fts_name
    assert c_b.fts_name == ""  # untouched
