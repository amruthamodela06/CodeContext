"""sentence-transformers embedder (CPU). Default: bge-small-en-v1.5 (ADR 0009)."""

from __future__ import annotations

import logging
from functools import cached_property
from typing import TYPE_CHECKING

from app.embeddings.protocol import Embedder

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

log = logging.getLogger(__name__)

# Known dimensions, so `dimension` doesn't require loading the model.
_DIMENSIONS: dict[str, int] = {
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-base-en-v1.5": 768,
}


class SentenceTransformersEmbedder(Embedder):
    def __init__(self, model_name: str) -> None:
        if model_name not in _DIMENSIONS:
            raise ValueError(f"unknown sentence-transformers model: {model_name!r}")
        self._model_name = model_name

    @property
    def name(self) -> str:
        # Strip the org prefix: "BAAI/bge-small-en-v1.5" -> "bge-small-en-v1.5".
        return self._model_name.split("/")[-1]

    @property
    def dimension(self) -> int:
        return _DIMENSIONS[self._model_name]

    @cached_property
    def _model(self) -> SentenceTransformer:
        # Lazy: the ~130 MB model loads on first use, not at import. Cached
        # under the HF cache (./.hf-cache volume in docker; host cache in dev).
        from sentence_transformers import SentenceTransformer

        log.info("loading embedding model %s (first use, CPU)", self._model_name)
        return SentenceTransformer(self._model_name, device="cpu")

    def embed_one(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        # normalize_embeddings=True → unit vectors, so cosine distance is clean.
        vectors = self._model.encode(
            texts,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [v.tolist() for v in vectors]
