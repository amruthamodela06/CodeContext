"""LLMProvider stubs for paid providers landing in a later ablation slice.

The interface is in place (so LLM_PROVIDER=openai/anthropic resolves) but the
calls raise NotImplementedError. ADR 0007 keeps these free-tier-default-only for
the deployed path; they exist for the cost/quality ablation in Slice 7.
"""

from collections.abc import AsyncIterator

from app.llm.protocol import GenResult, LLMProvider, Message

_MSG = "{name} LLM provider is an ablation-slice task (not implemented in v1)"


class OpenAIProvider(LLMProvider):
    @property
    def name(self) -> str:
        return "gpt-4o-mini"

    async def generate(self, messages: list[Message], **opts) -> GenResult:
        raise NotImplementedError(_MSG.format(name="OpenAI"))

    async def generate_stream(self, messages: list[Message], **opts) -> AsyncIterator[str]:
        raise NotImplementedError(_MSG.format(name="OpenAI"))
        yield ""  # unreachable — makes this an async generator, not a coroutine


class AnthropicProvider(LLMProvider):
    @property
    def name(self) -> str:
        return "claude-haiku"

    async def generate(self, messages: list[Message], **opts) -> GenResult:
        raise NotImplementedError(_MSG.format(name="Anthropic"))

    async def generate_stream(self, messages: list[Message], **opts) -> AsyncIterator[str]:
        raise NotImplementedError(_MSG.format(name="Anthropic"))
        yield ""  # unreachable — makes this an async generator, not a coroutine
