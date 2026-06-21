"""QueryClassifier interface + result type. See ADR 0012.

Routes incoming queries to one of five categories that drive retrieval
strategy (Slice 5g). Two concrete implementations land in this slice:
- KeywordClassifier (default): pure-Python, sub-ms, no LLM call.
- LLMClassifier (opt-in via QUERY_CLASSIFIER env): one extra LLM call per
  query; better accuracy on ambiguous phrasings at the cost of latency.

The classifier ALWAYS returns a result with a category + confidence; the
routing layer (POST /query) decides what to do with low-confidence
results (graceful degradation: run both flat + multi-hop pipelines and
merge). Out-of-scope detection here is one of two layers -- the §6.2
retrieval-confidence check still runs after retrieval to catch
in-scope-phrasing-but-no-supporting-code queries.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal

from pydantic import BaseModel, Field

Category = Literal[
    "lookup",  # "find X" / "where is Y"
    "architectural",  # "how does X work" / "what's the design of Y"
    "historical_why",  # "why was X added" -- routes to multi-hop graph traversal
    "impact",  # "what calls X" / "who depends on Y"
    "out_of_scope",  # off-topic / recipes / jailbreaks
]


class ClassificationResult(BaseModel):
    category: Category
    confidence: float = Field(ge=0.0, le=1.0)
    # Identifies which classifier produced this (debug + eval).
    method: str
    # True if the primary classifier failed to produce a usable result and
    # we fell back to the keyword classifier internally. Surfaces an LLM
    # parse failure (or any other auto-recovery) without requiring the
    # caller to inspect logs.
    fallback_used: bool = False


class QueryClassifier(ABC):
    """One implementation per classifier strategy. See ADR 0012."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Canonical identifier, e.g. 'keyword' or 'llm:gemini-2.0-flash'."""

    @abstractmethod
    async def classify(self, query: str) -> ClassificationResult:
        """Return the category + confidence for one query."""
