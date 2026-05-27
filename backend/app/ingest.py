"""Ingestion logic: URL parsing, git clone, filesystem walk, language detection, DB upsert.

Single module per CLAUDE.md "one module = one responsibility" — the responsibility
here is the entire repo-ingestion pipeline. Stays well under the 300-line splitting
threshold.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import File, Repo

log = logging.getLogger(__name__)

# --- URL parsing ----------------------------------------------------------

# GitHub allows owner names of 1-39 chars (alphanumeric + hyphen, not leading/trailing
# hyphen), and repo names of alphanumerics + `.`, `_`, `-`. Optional `.git` suffix
# and optional trailing slash.
_GITHUB_URL = re.compile(
    r"^https://github\.com/"
    r"([A-Za-z0-9](?:[A-Za-z0-9-]{0,38})?)/"
    r"([A-Za-z0-9_.\-]+?)"
    r"(?:\.git)?/?$"
)


def parse_github_url(url: str) -> tuple[str, str]:
    """Parse a public GitHub URL. Returns (owner, name) or raises ValueError."""
    match = _GITHUB_URL.match(url.strip())
    if not match:
        raise ValueError(f"not a recognized public GitHub URL: {url!r}")
    return match.group(1), match.group(2)


# --- Filter policy (ADR 0004) ---------------------------------------------

SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        "vendor",
        ".venv",
        "venv",
        "dist",
        "build",
        "__pycache__",
        "target",
        ".next",
        ".nuxt",
        ".cache",
        ".idea",
        ".vscode",
    }
)

SKIP_FILES: frozenset[str] = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "Pipfile.lock",
        "poetry.lock",
        "uv.lock",
        "Cargo.lock",
        "go.sum",
        "composer.lock",
        "Gemfile.lock",
    }
)

BINARY_EXTS: frozenset[str] = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
        ".pdf", ".zip", ".tar", ".gz", ".bz2", ".7z",
        ".exe", ".dll", ".so", ".dylib", ".o", ".a", ".wasm",
        ".ttf", ".otf", ".woff", ".woff2",
        ".mp3", ".mp4", ".mov", ".webm", ".ogg", ".flac",
        ".class", ".jar",
    }
)  # fmt: skip

MAX_FILE_SIZE: int = 1 * 1024 * 1024  # 1 MiB
MAX_FILE_COUNT: int = 5000  # PRD §6.1 ceiling


def should_skip(path: Path, repo_root: Path) -> bool:
    """Per-file skip decision applied during the walk."""
    rel = path.relative_to(repo_root)
    if any(part in SKIP_DIRS for part in rel.parts):
        return True
    if path.name in SKIP_FILES:
        return True
    if path.suffix.lower() in BINARY_EXTS:
        return True
    try:
        if path.stat().st_size > MAX_FILE_SIZE:
            return True
    except OSError:
        return True  # broken symlink or vanished
    return False


# --- Language detection ---------------------------------------------------

LANGUAGES: dict[str, str] = {
    ".py": "Python", ".pyi": "Python",
    ".ts": "TypeScript", ".tsx": "TypeScript",
    ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".kt": "Kotlin",
    ".rb": "Ruby",
    ".php": "PHP",
    ".c": "C", ".h": "C",
    ".cpp": "C++", ".cc": "C++", ".hpp": "C++",
    ".cs": "C#",
    ".swift": "Swift",
    ".md": "Markdown",
    ".rst": "reStructuredText",
    ".yml": "YAML", ".yaml": "YAML",
    ".toml": "TOML",
    ".json": "JSON",
    ".sql": "SQL",
    ".sh": "Shell", ".bash": "Shell",
    ".html": "HTML",
    ".css": "CSS", ".scss": "SCSS",
}  # fmt: skip


def detect_language(path: Path) -> str | None:
    """Return a human-readable language name, or None if unknown."""
    if path.name == "Dockerfile" or path.name.endswith(".dockerfile"):
        return "Dockerfile"
    if path.name == "Makefile":
        return "Makefile"
    return LANGUAGES.get(path.suffix.lower())


# --- Walk -----------------------------------------------------------------


@dataclass(frozen=True)
class FileMeta:
    path: str  # repo-relative, posix-style
    size_bytes: int
    language: str | None


def walk_repo(root: Path) -> list[FileMeta]:
    """Walk a cloned repo, applying the skip filter. Returns files sorted by path."""
    results: list[FileMeta] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune skip dirs in-place so we don't descend into them at all.
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for filename in filenames:
            full = Path(dirpath) / filename
            if should_skip(full, root):
                continue
            try:
                size = full.stat().st_size
            except OSError:
                continue
            rel = full.relative_to(root)
            results.append(
                FileMeta(
                    path=rel.as_posix(),
                    size_bytes=size,
                    language=detect_language(full),
                )
            )
    results.sort(key=lambda f: f.path)
    return results


# --- Git clone ------------------------------------------------------------


def clone_repo(clone_url: str, dest: Path) -> str:
    """Shallow-clone clone_url into dest. Returns the default branch name.

    Raises subprocess.CalledProcessError if `git` is missing or the clone fails.
    """
    subprocess.run(
        ["git", "clone", "--depth", "1", clone_url, str(dest)],
        check=True,
        capture_output=True,
        text=True,
    )
    head = subprocess.run(
        ["git", "-C", str(dest), "rev-parse", "--abbrev-ref", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return head.stdout.strip()


# --- Orchestrator ---------------------------------------------------------


class FileCountExceededError(Exception):
    """Raised when a repo's filtered file count exceeds MAX_FILE_COUNT."""

    def __init__(self, count: int) -> None:
        super().__init__(f"file count {count} exceeds limit {MAX_FILE_COUNT}")
        self.count = count


async def ingest_repo(url: str, session: AsyncSession) -> Repo:
    """Full ingestion pipeline: parse → clone → walk → upsert → chunk.

    Chunking is invoked after the file commit while the tempdir still exists
    (the chunker reads source files off disk this slice; see ADR 0008).
    Chunking failures are logged and swallowed — the ingest is preserved and
    chunks can be retried via POST /repos/{repo_id}/chunk.
    """
    # Lazy import to avoid an import cycle (app.api imports from app.ingest).
    from app.chunking.orchestrator import chunk_repo

    owner, name = parse_github_url(url)
    clone_url = f"https://github.com/{owner}/{name}.git"

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        dest = Path(tmp) / "repo"
        default_branch = await asyncio.to_thread(clone_repo, clone_url, dest)
        files = await asyncio.to_thread(walk_repo, dest)

        # Check ceiling BEFORE we touch the DB so a too-large repo causes no churn.
        if len(files) > MAX_FILE_COUNT:
            raise FileCountExceededError(len(files))

        # Upsert by delete-then-insert; FK cascade clears existing files.
        # Both DML operations live in the same transaction; an INSERT failure
        # rolls back the DELETE.
        existing = await session.scalar(select(Repo).where(Repo.owner == owner, Repo.name == name))
        if existing is not None:
            await session.delete(existing)
            await session.flush()

        repo = Repo(owner=owner, name=name, default_branch=default_branch)
        repo.files = [
            File(path=f.path, size_bytes=f.size_bytes, language=f.language) for f in files
        ]
        session.add(repo)
        await session.commit()

        # Chunk while the cloned tree is still on disk. Failures here don't
        # fail the ingest — the repo + files are already persisted.
        try:
            await chunk_repo(repo, dest, session)
        except Exception as exc:
            log.warning("chunking failed for %s/%s: %s", owner, name, exc)

    return repo
