"""Deterministic fake embedder for tests. No model, no ML overhead.

Uses a hashing bag-of-words: each token hashes to a dimension, value =
token count, then the vector is L2-normalized. This gives a real cosine
ordering invariant without a model — a chunk that shares tokens with the
query scores higher than one that doesn't. That lets `/search` ranking be
tested deterministically. It does NOT capture true semantics (synonyms,
paraphrase); the slow real-model integration test covers that.
"""

from __future__ import annotations

import hashlib
import math
import re

from app.embeddings.protocol import Embedder

_TOKEN = re.compile(r"\w+")


class FakeEmbedder(Embedder):
    def __init__(self, dim: int = 384) -> None:
        self._dim = dim

    @property
    def name(self) -> str:
        return "fake-embedder"

    @property
    def dimension(self) -> int:
        return self._dim

    def embed_one(self, text: str) -> list[float]:
        return self._vector(text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        for token in _TOKEN.findall(text.lower()):
            digest = hashlib.md5(token.encode("utf-8")).hexdigest()
            vec[int(digest, 16) % self._dim] += 1.0
        norm = math.sqrt(sum(x * x for x in vec))
        if norm == 0.0:
            # Empty / token-less text — return a fixed unit vector rather than
            # all-zeros (which would make cosine undefined).
            vec[0] = 1.0
            return vec
        return [x / norm for x in vec]
