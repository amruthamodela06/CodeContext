"""Parse PR title/body for issue cross-references. See ADR 0012.

GitHub treats specific keywords + ``#<number>`` as auto-closing references:
close / closes / closed, fix / fixes / fixed, resolve / resolves / resolved.
A bare ``#<number>`` (no keyword) is a *mention* — we DO NOT count that as
a references_issue edge; too many false positives ("see #234 for context",
"unlike #99 which...").

Cross-repo references (``owner/name#N``) are out of scope for v1 — the
issue would live in a different repo we haven't ingested.
"""

from __future__ import annotations

import re

# Keyword + optional separator + #<digits>. The keyword group covers all
# nine GitHub-recognized variants (3 stems x 3 tenses). Case-insensitive.
_ISSUE_REF = re.compile(
    r"\b(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?)\b[\s:-]*#(\d+)",
    re.IGNORECASE,
)


def extract_closing_issue_numbers(*texts: str | None) -> set[int]:
    """Return the deduped set of issue numbers this PR claims to close.

    Pass title and body (and any other text fields) as separate args —
    GitHub's auto-close honors keywords in title OR body.
    """
    out: set[int] = set()
    for t in texts:
        if not t:
            continue
        for m in _ISSUE_REF.finditer(t):
            try:
                out.add(int(m.group(1)))
            except ValueError:
                continue
    return out
