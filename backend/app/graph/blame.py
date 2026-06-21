"""Per-file ``git blame --line-porcelain`` parser.

The naive approach (one blame invocation per chunk) is O(chunks x git) and
slow on large repos. The right pattern is one invocation per *file* — blame
returns the SHA for every line; we look each chunk's start_line up in the
resulting map. ADR 0012 / PRD §9.5.

We keep the parser tolerant: an unknown file path, a chunk whose start_line
exceeds the file length, or a blame failure on one file all return None /
skip rather than aborting the whole graph build. The orchestrator records
warnings in graph_state for later inspection.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def blame_file(repo_root: Path, relative_path: str) -> dict[int, str] | None:
    """Run ``git blame --line-porcelain`` for one file inside the cloned repo.

    Returns ``{line_number: commit_sha}`` (1-indexed lines, matching how
    code_chunk.start_line is stored — ADR 0008). Returns None if the blame
    invocation fails (deleted file, broken symlink, binary file, etc.).
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "blame", "--line-porcelain", "--", relative_path],
            check=True,
            capture_output=True,
            text=True,
            # blame can hang on path encoding issues; treat them as a failure.
            errors="replace",
        )
    except subprocess.CalledProcessError:
        return None

    return _parse_porcelain(proc.stdout)


def _parse_porcelain(output: str) -> dict[int, str]:
    """Parse ``--line-porcelain`` output.

    Each line of the file emits a header block followed by a single ``\\t``-
    prefixed content line. The header's first line is::

        <sha> <orig-line> <final-line> [<group-size>]

    We need only ``sha`` and ``final-line``.
    """
    line_to_sha: dict[int, str] = {}
    for header_line in output.split("\n"):
        # Only the header (first line of each block) starts with a 40-char sha
        # followed by a space; subsequent header lines are key/value pairs
        # (author/author-mail/etc) and the content line starts with a tab.
        if len(header_line) > 41 and header_line[40] == " " and _is_sha(header_line[:40]):
            parts = header_line.split(" ")
            # parts: [sha, orig_line, final_line, group_size?]
            if len(parts) >= 3:
                try:
                    final_line = int(parts[2])
                except ValueError:
                    continue
                line_to_sha[final_line] = parts[0]
    return line_to_sha


def _is_sha(s: str) -> bool:
    return len(s) == 40 and all(c in "0123456789abcdef" for c in s)


def fetch_commit_stub(repo_root: Path, sha: str) -> dict | None:
    """Return a minimal commit dict for an unknown SHA (one outside the
    GraphQL-ingested window). Used by the orchestrator to insert a stub
    commit row so the introduced_by edge always has a valid target.

    Returns None if the SHA isn't reachable from the local clone (rare —
    shallow clones miss old history, but we re-cloned full-depth above
    blame's call site).
    """
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "log",
                "-1",
                "--format=%an%x00%ae%x00%aI%x00%cI%x00%B",
                sha,
            ],
            check=True,
            capture_output=True,
            text=True,
            errors="replace",
        )
    except subprocess.CalledProcessError:
        return None
    parts = proc.stdout.split("\x00", 4)
    if len(parts) != 5:
        return None
    author_name, author_email, authored_at, committed_at, message = parts
    return {
        "sha": sha,
        "author_name": author_name or None,
        "author_email": author_email or None,
        "authored_at": authored_at or None,
        "committed_at": committed_at or None,
        "message": message.rstrip("\n"),
    }
