"""Shared transport for providers exposing an OpenAI-compatible API.

Gemini and Ollama both speak the OpenAI /chat/completions protocol, so a single
AsyncOpenAI client (keyed by base_url + api_key) backs both — no per-provider
HTTP code (ADR 0007 + 0010). Provider-specific behavior (key checks, rate
limiting) goes in the `_before_request` hook, overridden by subclasses.
"""

from collections.abc import AsyncIterator

from openai import AsyncOpenAI

from app.llm.protocol import GenResult, LLMProvider, Message


class _OpenAICompatProvider(LLMProvider):
    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        default_extra: dict | None = None,
    ) -> None:
        self._model = model
        # The SDK requires a non-empty key even when the server ignores it
        # (Ollama). A placeholder keeps local/offline use key-free.
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key or "not-needed")
        self._default_extra = default_extra or {}

    @property
    def name(self) -> str:
        return self._model

    async def _before_request(self) -> None:
        """Hook for subclasses (rate limiting, key checks). No-op by default."""

    def _request_kwargs(
        self,
        messages: list[Message],
        max_tokens: int,
        temperature: float,
        stop: list[str] | None,
        extra: dict | None,
    ) -> dict:
        kwargs: dict = {
            "model": self._model,
            "messages": [m.model_dump() for m in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if stop:
            kwargs["stop"] = stop
        # Provider defaults first (e.g. Ollama num_ctx), then per-call overrides.
        kwargs.update(self._default_extra)
        if extra:
            kwargs.update(extra)
        return kwargs

    async def generate(
        self,
        messages: list[Message],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.2,
        stop: list[str] | None = None,
        extra: dict | None = None,
    ) -> GenResult:
        await self._before_request()
        resp = await self._client.chat.completions.create(
            **self._request_kwargs(messages, max_tokens, temperature, stop, extra),
            stream=False,
        )
        choice = resp.choices[0]
        usage = resp.usage
        return GenResult(
            text=choice.message.content or "",
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )

    async def generate_stream(
        self,
        messages: list[Message],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.2,
        stop: list[str] | None = None,
        extra: dict | None = None,
    ) -> AsyncIterator[str]:
        await self._before_request()
        stream = await self._client.chat.completions.create(
            **self._request_kwargs(messages, max_tokens, temperature, stop, extra),
            stream=True,
        )
        try:
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    yield delta.content
        finally:
            # Close the upstream connection on completion OR cancellation, so a
            # client disconnect doesn't leak a connection / burn quota (ADR 0010).
            await stream.close()
