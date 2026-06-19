"""Ollama (local Qwen 2.5 Coder) via its OpenAI-compatible endpoint.

Offline / ablation provider — no API key, no rate limit. `num_ctx` is set
explicitly through `extra_body.options` because Ollama's default context window
(2k) truncates the retrieved excerpts. See ADR 0010 + the README platform note.
"""

from app.config import get_settings
from app.llm.openai_compat import _OpenAICompatProvider

# Qwen 2.5 Coder 3B fits a 16k window comfortably on the CPU-only dev box.
_NUM_CTX = 16384


class OllamaProvider(_OpenAICompatProvider):
    def __init__(self) -> None:
        s = get_settings()
        super().__init__(
            model=s.ollama_model,
            base_url=s.ollama_base_url,
            api_key="ollama",  # ignored by the server, required by the SDK
            default_extra={"extra_body": {"options": {"num_ctx": _NUM_CTX}}},
        )
