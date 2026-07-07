"""Full-text search content composition for code chunks. See ADR 0014.

Chunks store three app-managed TEXT intermediates (fts_name / fts_doc /
fts_body); Postgres compiles them into a weighted tsvector via a
generated column (Slice 6a). This module owns the *composition* logic
-- the chunker calls ``compute_chunk_fts`` at ingest time, and the
Slice-6b backfill calls ``backfill_chunk_fts`` to populate historical
rows in place.

Why the split lives in Python, not SQL: the English tokenizer that
Postgres runs at index time treats ``getUserByEmail`` as one lexeme
(``getuserbyemail``), so a query like ``user email lookup`` won't
match. Splitting camelCase / snake_case / hyphen boundaries at index
time and appending the split tokens *alongside* the original
identifier restores the compositional match while preserving
exact-name lookups.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CodeChunk

_CAMEL_LOWER_UPPER = re.compile(r"([a-z0-9])([A-Z])")
_CAMEL_UPPER_LOWER = re.compile(r"([A-Z]+)([A-Z][a-z])")

# Python: match the leading `def foo(...)` / `class Foo` / `async def foo`
# and capture the immediately-following triple-quoted string literal, if any.
_PY_DOCSTRING = re.compile(
    r"^\s*(?:async\s+)?(?:def|class)\s+\w+[^:]*:\s*"
    r"(?P<q>\"\"\"|''')(?P<doc>.*?)(?P=q)",
    re.DOTALL | re.MULTILINE,
)


def split_identifier(name: str) -> str:
    """Return ``name`` verbatim plus its camelCase / snake_case / hyphen splits.

    Examples::

        getUserByEmail       -> "getUserByEmail get User by Email"
        parse_html_response  -> "parse_html_response parse html response"
        HTTPServer           -> "HTTPServer HTTP Server"
        HTTPSProxy           -> "HTTPSProxy HTTPS Proxy"

    Original identifier is preserved so exact-name queries still hit; the
    subtokens are additive. Single-character fragments are dropped as noise.
    Empty input returns empty string. Case is left as-is; the Postgres
    english tokenizer lowercases + stems at index time.
    """
    if not name:
        return ""
    s = _CAMEL_LOWER_UPPER.sub(r"\1 \2", name)
    s = _CAMEL_UPPER_LOWER.sub(r"\1 \2", s)
    parts = re.split(r"[\s_\-]+", s)
    subtokens = [p for p in parts if len(p) >= 2]
    if not subtokens:
        return name
    return f"{name} {' '.join(subtokens)}"


def extract_docstring(content: str, language: str) -> str:
    """Extract the leading docstring / doc-comment from a chunk. ``""`` on miss.

    Python only for v1 -- matches the current chunker (ADR 0008). Adding
    TS/JS (``/**`` JSDoc), Go (``//`` leading comments), or Rust (``///``
    / ``//!``) is a per-language extractor when those chunkers land.
    """
    if language == "Python":
        m = _PY_DOCSTRING.search(content)
        if m is not None:
            return m.group("doc").strip()
    return ""


def compute_chunk_fts(
    *,
    name: str | None,
    parent_name: str | None,
    content: str,
    language: str,
) -> tuple[str, str, str]:
    """Return ``(fts_name, fts_doc, fts_body)`` for a chunk.

    Pure function -- deliberately no session / row coupling so the chunker
    and the backfill can share it. ``fts_body`` is the raw content
    verbatim; the tsvector's D-weight bucket makes it a fallback that
    only helps when name + docstring miss the query.
    """
    parts: list[str] = []
    if parent_name:
        parts.append(split_identifier(parent_name))
    if name:
        parts.append(split_identifier(name))
    fts_name = " ".join(p for p in parts if p)
    fts_doc = extract_docstring(content, language)
    fts_body = content
    return fts_name, fts_doc, fts_body


async def backfill_chunk_fts(session: AsyncSession, repo_id: int, *, batch_size: int = 500) -> int:
    """Populate fts_name/doc/body on this repo's existing chunks. Idempotent.

    Walks chunks in id-order batches so a mid-run failure resumes cleanly on
    the next call (the batches after the crash are re-scanned but the
    computed fields overwrite deterministically). Returns the count of rows
    written.
    """
    total = 0
    last_id = 0
    while True:
        rows: Iterable[CodeChunk] = (
            (
                await session.execute(
                    select(CodeChunk)
                    .where(CodeChunk.repo_id == repo_id, CodeChunk.id > last_id)
                    .order_by(CodeChunk.id)
                    .limit(batch_size)
                )
            )
            .scalars()
            .all()
        )
        rows = list(rows)
        if not rows:
            break
        for c in rows:
            c.fts_name, c.fts_doc, c.fts_body = compute_chunk_fts(
                name=c.name,
                parent_name=c.parent_name,
                content=c.content,
                language=c.language,
            )
        await session.commit()
        total += len(rows)
        last_id = rows[-1].id
    return total
