"""Embedder interface. See ADR 0009."""

from abc import ABC, abstractmethod


class Embedder(ABC):
    """Turns text into dense vectors. One implementation per provider.

    `dimension` is load-bearing: the pgvector column is `vector(N)` fixed at
    migration time, so the active embedder's dimension must match the column.
    A startup check asserts this (see app.main lifespan).
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Canonical model identifier, e.g. 'bge-small-en-v1.5'."""

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Output vector dimension."""

    @abstractmethod
    def embed_one(self, text: str) -> list[float]:
        """Embed a single text."""

    @abstractmethod
    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch. Implementations should batch the underlying call."""
