"""Query classifier package. See ADR 0012.

Mirrors the app.embeddings + app.llm factory pattern: @cache'd builder
keyed on QUERY_CLASSIFIER env var, one instance per resolved name per
process.
"""

from functools import cache

from app.classifier.keyword import KeywordClassifier
from app.classifier.llm import LLMClassifier
from app.classifier.protocol import Category, ClassificationResult, QueryClassifier
from app.config import get_settings
from app.llm import get_llm_provider


@cache
def _build(name: str) -> QueryClassifier:
    match name:
        case "keyword":
            return KeywordClassifier()
        case "llm":
            return LLMClassifier(get_llm_provider())
        case _:
            raise ValueError(f"unknown QUERY_CLASSIFIER: {name!r}")


def get_classifier(name: str | None = None) -> QueryClassifier:
    """Return the configured QueryClassifier (cached per resolved name)."""
    resolved = name or get_settings().query_classifier
    return _build(resolved)


__all__ = [
    "Category",
    "ClassificationResult",
    "KeywordClassifier",
    "LLMClassifier",
    "QueryClassifier",
    "get_classifier",
]
