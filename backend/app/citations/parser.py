"""Extracts [chunk:xx] citation tokens from an LLM answer, skipping code.

A line-oriented state machine tracks fenced code blocks (``` and ~~~, variable
length) so citations inside code are ignored; inline code spans are blanked per
line before token extraction. The token regex matches SHAPE only -- it never
checks whether an id is real. Membership is the validator's job, so a
well-formed-but-unknown id (e.g. c99) is still surfaced (to be flagged invalid)
rather than silently dropped. See ADR 0010.
"""

import re
from dataclasses import dataclass

# Shape-only. Accepts the `none` sentinel or 1-16 chars of [A-Za-z0-9_-].
# Rejects empty ([chunk:]), whitespace-padded ([chunk: c1 ]), and unterminated
# ([chunk:c1) forms by construction.
_TOKEN_RE = re.compile(r"\[chunk:(none|[A-Za-z0-9_-]{1,16})\]")
# A fence opener/closer is a line whose first non-space content is >=3 backticks
# or >=3 tildes.
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})")


@dataclass(frozen=True)
class ParsedCitation:
    display_id: str  # "c1" or "none"
    start: int  # char offset of "[" in the original answer
    end: int  # char offset just past "]"


def _blank_inline_code(text: str) -> str:
    """Replace inline code spans with spaces, preserving length (so offsets into
    the original line stay valid). CommonMark: a span opens with a run of N
    backticks and closes with the next run of EXACTLY N backticks; a longer run
    does not close it, and an unclosed run is treated as literal backticks.
    """
    out = list(text)
    n = len(text)
    i = 0
    while i < n:
        if text[i] != "`":
            i += 1
            continue
        j = i
        while j < n and text[j] == "`":
            j += 1
        run = j - i
        k = j
        closed = False
        while k < n:
            if text[k] == "`":
                m = k
                while m < n and text[m] == "`":
                    m += 1
                if m - k == run:
                    for p in range(i, m):
                        out[p] = " "
                    i = m
                    closed = True
                    break
                k = m
            else:
                k += 1
        if not closed:
            i = j  # no closing run; the opening backticks are literal
    return "".join(out)


def parse(answer: str) -> tuple[list[ParsedCitation], list[str]]:
    """Return (citations, warnings). Warnings currently: 'fence_unterminated'."""
    citations: list[ParsedCitation] = []
    warnings: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    offset = 0
    for line in answer.splitlines(keepends=True):
        marker = _FENCE_RE.match(line.lstrip())
        if marker:
            text = marker.group(1)
            ch, length = text[0], len(text)
            if not in_fence:
                in_fence, fence_char, fence_len = True, ch, length
            elif ch == fence_char and length >= fence_len:
                in_fence = False
            offset += len(line)  # fence delimiter lines never carry citations
            continue
        if in_fence:
            offset += len(line)
            continue
        scrubbed = _blank_inline_code(line)
        for tok in _TOKEN_RE.finditer(scrubbed):
            citations.append(
                ParsedCitation(
                    display_id=tok.group(1),
                    start=offset + tok.start(),
                    end=offset + tok.end(),
                )
            )
        offset += len(line)
    if in_fence:
        warnings.append("fence_unterminated")
    return citations, warnings
