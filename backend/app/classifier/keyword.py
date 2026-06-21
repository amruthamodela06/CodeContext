"""Pure-Python keyword classifier. Default per the PRD §7 latency budget.

Patterns are matched in priority order — `historical_why` first because it's
the most specific (and the most expensive to mis-route, since it triggers
multi-hop expansion). A query with no pattern match falls back to `lookup`
with low confidence (0.3) — the /query layer treats sub-threshold confidence
as "run both flat + multi-hop and merge" (graceful degradation).
"""

from __future__ import annotations

import re

from app.classifier.protocol import Category, ClassificationResult, QueryClassifier

# Each tuple: (category, regex, confidence). Order matters — first match wins.
# Confidences are calibrated against the ad-hoc fixture set in test_classifier.py;
# Slice 7 eval will tighten them against the real eval queries.
_PATTERNS: list[tuple[Category, re.Pattern[str], float]] = [
    # historical_why -- "why", rationale, decision, motivation, history-tinted verbs.
    (
        "historical_why",
        re.compile(
            r"\b(why|rationale|decision|reason|motivat|history|origin|introduce[ds]?|"
            r"added|removed|changed)\b",
            re.IGNORECASE,
        ),
        0.9,
    ),
    # impact -- callers / dependents / usage. Covers "what calls X" and the
    # more natural "what functions call X" / "what code calls X" variants.
    (
        "impact",
        re.compile(
            r"\b(what\s+\w+\s+calls?|what\s+calls|"
            r"who\s+(?:calls|uses|depends)|"
            r"callers?\s+of|called\s+by|"
            r"depends?\s+on|uses?\s+of|usage\s+of|"
            r"consumers?\s+of|impact\s+of)\b",
            re.IGNORECASE,
        ),
        0.85,
    ),
    # architectural -- system-level understanding from code alone.
    (
        "architectural",
        re.compile(
            r"\b(how\s+does|architecture|design|structure|flow|overview|"
            r"interact[s]?\s+with|relationship\s+between)\b",
            re.IGNORECASE,
        ),
        0.8,
    ),
    # lookup -- locate a specific element.
    (
        "lookup",
        re.compile(
            r"\b(where\s+is|find|locate|definition\s+of|implementation\s+of|"
            r"defined\s+(in|at)|show\s+me)\b",
            re.IGNORECASE,
        ),
        0.8,
    ),
]


class KeywordClassifier(QueryClassifier):
    @property
    def name(self) -> str:
        return "keyword"

    async def classify(self, query: str) -> ClassificationResult:
        for category, pattern, confidence in _PATTERNS:
            if pattern.search(query):
                return ClassificationResult(
                    category=category, confidence=confidence, method=self.name
                )
        # No keyword matched -- default to lookup with low confidence so the
        # /query router knows to use graceful degradation (run both flat +
        # multi-hop pipelines and merge).
        return ClassificationResult(category="lookup", confidence=0.3, method=self.name)
