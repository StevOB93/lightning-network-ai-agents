import os

from ai.llm.base import LLMBackend


def create_backend() -> LLMBackend:
    """
    Factory returns a provider-specific backend that implements LLMBackend.
    Agent core stays provider-agnostic.
    """
    provider = os.getenv("LLM_PROVIDER", "gemini").strip().lower()

    if provider == "gemini":
        from ai.llm.adapters.gemini_backend import GeminiBackend
        return GeminiBackend()

    if provider == "openai":
        from ai.llm.adapters.openai_backend import OpenAIBackend
        return OpenAIBackend()

    raise ValueError(f"Unsupported LLM_PROVIDER: {provider}")
