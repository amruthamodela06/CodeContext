"""Embedder stubs for providers landing in a later slice (ADR 0007 ablation).

Each exposes `name` + `dimension` (so the interface is documented) but raises
NotImplementedError on the embed calls.
"""

from app.embeddings.protocol import Embedder


class OpenAIEmbedder(Embedder):
    """OpenAI text-embedding-3-small (1536-dim). Paid; ablation only."""

    @property
    def name(self) -> str:
        return "text-embedding-3-small"

    @property
    def dimension(self) -> int:
        return 1536

    def embed_one(self, text: str) -> list[float]:
        raise NotImplementedError("OpenAI embedder is an ablation-slice task")

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError("OpenAI embedder is an ablation-slice task")


class VoyageEmbedder(Embedder):
    """Voyage voyage-code-2 (1536-dim), code-specialized. Paid; ablation only."""

    @property
    def name(self) -> str:
        return "voyage-code-2"

    @property
    def dimension(self) -> int:
        return 1536

    def embed_one(self, text: str) -> list[float]:
        raise NotImplementedError("Voyage embedder is an ablation-slice task")

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError("Voyage embedder is an ablation-slice task")


class OllamaEmbedder(Embedder):
    """Ollama nomic-embed-text (768-dim), local. Ablation / offline only."""

    @property
    def name(self) -> str:
        return "nomic-embed-text"

    @property
    def dimension(self) -> int:
        return 768

    def embed_one(self, text: str) -> list[float]:
        raise NotImplementedError("Ollama embedder is an ablation-slice task")

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError("Ollama embedder is an ablation-slice task")
