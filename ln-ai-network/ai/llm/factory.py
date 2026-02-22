import os

from ai.llm.base import LLMBackend
from ai.llm.adapters.openai_backend import OpenAIBackend


def create_backend() -> LLMBackend:
    """
    Factory returns a provider-specific backend that implements LLMBackend.
    Agent core stays provider-agnostic.
    """
    provider = os.getenv("LLM_PROVIDER", "openai").strip().lower()

    if provider == "openai":
        return OpenAIBackend()

    raise ValueError(f"Unsupported LLM_PROVIDER: {provider}")