"""LLM generation + provider factory. See ADR 0007 + ADR 0010.

Mirrors app.embeddings: a @cache'd factory keyed on the resolved provider id, so
each provider (and its AsyncOpenAI client) is built once per process.
"""

from functools import cache

from app.config import get_settings
from app.llm.fake import MockLLMProvider
from app.llm.gemini import GeminiProvider
from app.llm.ollama import OllamaProvider
from app.llm.protocol import GenResult, LLMProvider, Message
from app.llm.stubs import AnthropicProvider, OpenAIProvider


@cache
def _build(provider: str) -> LLMProvider:
    match provider:
        case "gemini":
            return GeminiProvider()
        case "ollama":
            return OllamaProvider()
        case "mock":
            return MockLLMProvider()
        case "openai":
            return OpenAIProvider()
        case "anthropic":
            return AnthropicProvider()
        case _:
            raise ValueError(f"unknown LLM_PROVIDER: {provider!r}")


def get_llm_provider(provider: str | None = None) -> LLMProvider:
    """Return the configured LLMProvider (cached per resolved provider id)."""
    resolved = provider or get_settings().llm_provider
    return _build(resolved)


__all__ = [
    "GenResult",
    "LLMProvider",
    "Message",
    "MockLLMProvider",
    "get_llm_provider",
]
