"""LLM-backed query classifier (opt-in via QUERY_CLASSIFIER=llm).

Strict JSON output -- a parse failure or unknown category falls back to
the KeywordClassifier and marks the result with ``fallback_used=True`` so
the routing layer sees that the primary classifier didn't produce a
trusted result. The /query layer treats fallback OR low confidence as a
signal to run both pipelines and merge (graceful degradation, ADR 0012).
"""

from __future__ import annotations

import json
import logging
from typing import get_args

from app.classifier.keyword import KeywordClassifier
from app.classifier.prompts import LLM_CLASSIFIER_PROMPT
from app.classifier.protocol import Category, ClassificationResult, QueryClassifier
from app.llm.protocol import LLMProvider, Message

log = logging.getLogger(__name__)

_VALID_CATEGORIES = set(get_args(Category))


class LLMClassifier(QueryClassifier):
    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider
        # Keyword classifier is the always-available fallback path.
        self._fallback = KeywordClassifier()

    @property
    def name(self) -> str:
        return f"llm:{self._provider.name}"

    async def classify(self, query: str) -> ClassificationResult:
        prompt = LLM_CLASSIFIER_PROMPT.format(query=query.replace('"', '\\"'))
        try:
            gen = await self._provider.generate(
                [Message(role="user", content=prompt)],
                max_tokens=40,
                temperature=0.0,
            )
        except Exception as exc:
            log.warning("LLM classifier failed (%s); falling back to keyword", exc)
            fb = await self._fallback.classify(query)
            return fb.model_copy(update={"method": self.name, "fallback_used": True})

        parsed = _try_parse(gen.text)
        if parsed is None:
            log.warning(
                "LLM classifier returned unparseable output (%r); falling back",
                gen.text[:100],
            )
            fb = await self._fallback.classify(query)
            return fb.model_copy(update={"method": self.name, "fallback_used": True})

        return ClassificationResult(
            category=parsed["category"],
            confidence=parsed["confidence"],
            method=self.name,
        )


def _try_parse(text: str) -> dict | None:
    """Return a validated {category, confidence} dict, or None.

    Tolerates leading/trailing whitespace, prose, or markdown fences -- we
    grab the first ``{...}`` block and try json.loads.
    """
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    category = obj.get("category")
    confidence = obj.get("confidence")
    if category not in _VALID_CATEGORIES:
        return None
    if not isinstance(confidence, int | float) or not (0.0 <= confidence <= 1.0):
        return None
    return {"category": category, "confidence": float(confidence)}
