"""Tests for Slice 5e -- query classifier (keyword + LLM)."""

from __future__ import annotations

import pytest

from app.classifier import (
    ClassificationResult,
    KeywordClassifier,
    LLMClassifier,
)
from app.classifier.llm import _try_parse
from app.llm.protocol import GenResult, LLMProvider, Message

# --- KeywordClassifier ------------------------------------------------------


@pytest.mark.parametrize(
    "query, expected",
    [
        # historical_why -- the most specific, matched first
        ("Why was retry logic added to the database client?", "historical_why"),
        ("What's the rationale for switching to bcrypt?", "historical_why"),
        ("When was async support introduced?", "historical_why"),
        # impact
        ("What functions call validate_user_input?", "impact"),
        ("Who depends on the legacy auth module?", "impact"),
        ("What are the callers of syncify?", "impact"),
        # architectural
        ("How does the request middleware chain work?", "architectural"),
        ("What's the overall design of the routing layer?", "architectural"),
        # lookup
        ("Where is the JWT validation function?", "lookup"),
        ("Show me the implementation of bcrypt_hash", "lookup"),
        # No keyword match -- default to lookup with low confidence
        ("Authentication module", "lookup"),
    ],
)
async def test_keyword_classifier_routes_query(query: str, expected: str) -> None:
    result = await KeywordClassifier().classify(query)
    assert result.category == expected
    assert result.method == "keyword"


async def test_keyword_classifier_low_confidence_when_no_match() -> None:
    result = await KeywordClassifier().classify("Authentication module")
    assert result.confidence < 0.5  # graceful-degradation trigger


async def test_keyword_classifier_historical_why_beats_lookup() -> None:
    """Priority order matters: a query mentioning 'why' but also 'find'
    should route to historical_why, not lookup."""
    result = await KeywordClassifier().classify("Find why MD5 was deprecated")
    assert result.category == "historical_why"


# --- LLMClassifier._try_parse (pure-logic JSON tolerance) ------------------


def test_try_parse_handles_strict_json():
    assert _try_parse('{"category": "lookup", "confidence": 0.9}') == {
        "category": "lookup",
        "confidence": 0.9,
    }


def test_try_parse_handles_leading_prose():
    text = 'Here is the answer:\n{"category": "historical_why", "confidence": 0.85}'
    assert _try_parse(text) == {
        "category": "historical_why",
        "confidence": 0.85,
    }


def test_try_parse_handles_markdown_fences():
    text = '```json\n{"category": "impact", "confidence": 0.92}\n```'
    assert _try_parse(text) == {"category": "impact", "confidence": 0.92}


def test_try_parse_rejects_invalid_category():
    assert _try_parse('{"category": "bogus", "confidence": 0.9}') is None


def test_try_parse_rejects_out_of_range_confidence():
    assert _try_parse('{"category": "lookup", "confidence": 1.5}') is None
    assert _try_parse('{"category": "lookup", "confidence": -0.1}') is None


def test_try_parse_rejects_malformed():
    assert _try_parse("not json at all") is None
    assert _try_parse('{"category": "lookup"}') is None  # missing confidence


# --- LLMClassifier with stub providers --------------------------------------


class _StubProvider:
    """Minimal stand-in for an LLMProvider; returns canned text."""

    def __init__(self, text: str, *, raise_on_call: Exception | None = None) -> None:
        self._text = text
        self._raise = raise_on_call

    @property
    def name(self) -> str:
        return "stub"

    async def generate(self, messages: list[Message], **opts) -> GenResult:
        if self._raise:
            raise self._raise
        return GenResult(text=self._text)

    async def generate_stream(self, messages, **opts):  # unused but required
        yield ""


async def test_llm_classifier_returns_parsed_result() -> None:
    provider: LLMProvider = _StubProvider('{"category": "historical_why", "confidence": 0.93}')
    result = await LLMClassifier(provider).classify("Why was X done?")
    assert result.category == "historical_why"
    assert result.confidence == 0.93
    assert result.method == "llm:stub"
    assert result.fallback_used is False


async def test_llm_classifier_falls_back_on_parse_failure() -> None:
    provider: LLMProvider = _StubProvider("definitely not json")
    # query has the "why" keyword -> keyword fallback returns historical_why.
    result = await LLMClassifier(provider).classify("Why does X happen?")
    assert result.category == "historical_why"
    assert result.method == "llm:stub"  # surfaces the configured method...
    assert result.fallback_used is True  # ... but flags the fallback


async def test_llm_classifier_falls_back_on_provider_error() -> None:
    provider: LLMProvider = _StubProvider("", raise_on_call=RuntimeError("upstream down"))
    result = await LLMClassifier(provider).classify("find auth")
    assert result.fallback_used is True
    assert result.category in {"lookup", "architectural", "historical_why", "impact"}


# --- Factory (env-based selection) ------------------------------------------


def test_factory_returns_keyword_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.classifier import _build, get_classifier

    _build.cache_clear()
    monkeypatch.delenv("QUERY_CLASSIFIER", raising=False)
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        c = get_classifier()
        assert isinstance(c, KeywordClassifier)
    finally:
        get_settings.cache_clear()
        _build.cache_clear()


def test_factory_explicit_keyword() -> None:
    from app.classifier import _build, get_classifier

    _build.cache_clear()
    c = get_classifier("keyword")
    assert isinstance(c, KeywordClassifier)


def test_factory_unknown_raises() -> None:
    from app.classifier import _build, get_classifier

    _build.cache_clear()
    with pytest.raises(ValueError, match="unknown QUERY_CLASSIFIER"):
        get_classifier("bogus")


# Smoke: ClassificationResult validates confidence range.
def test_result_validates_confidence_bounds() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ClassificationResult(category="lookup", confidence=1.5, method="x")
