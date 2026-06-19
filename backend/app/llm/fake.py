"""Deterministic in-process LLM for tests (LLM_PROVIDER=mock).

Returns a canned answer that exercises every citation status: a valid id (c1,
always present when >=1 chunk is retrieved), an out-of-range id (c99 -> invalid),
and the explicit [chunk:none] sentinel. Streaming yields the same text token by
token so the streaming and non-streaming paths reconstruct identically.
"""

from collections.abc import AsyncIterator

from app.llm.protocol import GenResult, LLMProvider, Message

_ANSWER = (
    "The `syncify` helper turns an async function into a blocking one [chunk:c1]. "
    "It is a popular utility [chunk:c99]. "
    "Event loops are a general Python concept [chunk:none]."
)


class MockLLMProvider(LLMProvider):
    @property
    def name(self) -> str:
        return "mock-llm"

    async def generate(self, messages: list[Message], **opts) -> GenResult:
        return GenResult(text=_ANSWER, prompt_tokens=0, completion_tokens=0)

    async def generate_stream(self, messages: list[Message], **opts) -> AsyncIterator[str]:
        words = _ANSWER.split(" ")
        for i, word in enumerate(words):
            yield word if i == len(words) - 1 else word + " "
