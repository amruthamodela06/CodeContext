"""Gemini via its OpenAI-compatible endpoint — the default provider (free tier).

Wrapped in a per-process RPM limiter (ADR 0010) so a dev hot-loop can't exhaust
the free tier. The API key is checked at call time (not construction) so the
factory can build the provider without a key present.
"""

import logging

from app.config import get_settings
from app.llm.openai_compat import _OpenAICompatProvider
from app.llm.ratelimit import RateLimiter

log = logging.getLogger(__name__)

# Google's OpenAI-compatibility shim. The trailing slash matters to the SDK.
_GEMINI_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"


class GeminiProvider(_OpenAICompatProvider):
    def __init__(self) -> None:
        s = get_settings()
        super().__init__(
            model=s.gemini_model,
            base_url=_GEMINI_OPENAI_BASE,
            api_key=s.gemini_api_key,
        )
        self._has_key = bool(s.gemini_api_key)
        self._limiter = RateLimiter(rpm=s.gemini_rpm_limit)

    async def _before_request(self) -> None:
        if not self._has_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set; required for LLM_PROVIDER=gemini. "
                "Get a free key from Google AI Studio or use LLM_PROVIDER=ollama."
            )
        await self._limiter.acquire()
