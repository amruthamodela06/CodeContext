"""LLMProvider interface. See ADR 0007 (provider abstraction) + ADR 0010 (citations).

Mirrors the Embedder ABC (app.embeddings.protocol): one implementation per
provider, selected by env via the get_llm_provider factory. Providers must
support both non-streaming (tests, the Slice 5 classifier) and streaming (the
user-facing /query endpoint) generation.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Literal

from pydantic import BaseModel

Role = Literal["system", "user", "assistant"]


class Message(BaseModel):
    role: Role
    content: str


class GenResult(BaseModel):
    """Non-streaming generation result plus token accounting (for cost ablation)."""

    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


class LLMProvider(ABC):
    """Turns a chat message list into text. Swappable via LLM_PROVIDER alone —
    no provider-specific code leaks into /query or the citation pipeline.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Canonical model identifier, e.g. 'gemini-2.0-flash'."""

    @abstractmethod
    async def generate(
        self,
        messages: list[Message],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.2,
        stop: list[str] | None = None,
        extra: dict | None = None,
    ) -> GenResult:
        """Non-streaming completion."""

    @abstractmethod
    def generate_stream(
        self,
        messages: list[Message],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.2,
        stop: list[str] | None = None,
        extra: dict | None = None,
    ) -> AsyncIterator[str]:
        """Streaming completion — yields text deltas as they arrive."""
