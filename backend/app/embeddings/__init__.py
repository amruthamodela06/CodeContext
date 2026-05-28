"""Embedding generation + provider factory. See ADR 0009."""

from functools import cache

from app.config import get_settings
from app.embeddings.fake import FakeEmbedder
from app.embeddings.protocol import Embedder
from app.embeddings.sentence_transformers import SentenceTransformersEmbedder
from app.embeddings.stubs import OllamaEmbedder, OpenAIEmbedder, VoyageEmbedder


@cache
def _build(provider: str) -> Embedder:
    match provider:
        case "bge-small":
            return SentenceTransformersEmbedder("BAAI/bge-small-en-v1.5")
        case "bge-base":
            return SentenceTransformersEmbedder("BAAI/bge-base-en-v1.5")
        case "mock":
            return FakeEmbedder(dim=384)
        case "openai":
            return OpenAIEmbedder()
        case "voyage":
            return VoyageEmbedder()
        case "ollama":
            return OllamaEmbedder()
        case _:
            raise ValueError(f"unknown EMBEDDING_PROVIDER: {provider!r}")


def get_embedder(provider: str | None = None) -> Embedder:
    """Return the configured Embedder (cached per resolved provider id).

    `provider` defaults to settings.embedding_provider. Caching means the
    sentence-transformers model is constructed once per provider id; the model
    weights themselves load lazily on first embed call.
    """
    resolved = provider or get_settings().embedding_provider
    return _build(resolved)


__all__ = ["Embedder", "FakeEmbedder", "get_embedder"]
