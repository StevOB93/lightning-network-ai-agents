# ai/llm/factory.py
from __future__ import annotations

import os


def _env(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    if v is None:
        return default
    v = v.strip()
    return v if v else default


def create_backend():
    """
    Select an LLM backend.

    Selection order:
      1) LLM_BACKEND / AI_LLM_BACKEND env var (case-insensitive)
      2) default to 'ollama'

    Supported values (typical):
      - 'ollama'
      - 'openai'

    This function intentionally avoids importing OpenAI backend unless selected,
    so missing OPENAI_API_KEY doesn't crash when you're using Ollama.
    """
    backend = (_env("LLM_BACKEND") or _env("AI_LLM_BACKEND") or "ollama").lower()

    if backend in ("ollama", "local", "ollama_backend"):
        try:
            from ai.llm.adapters.ollama_backend import OllamaBackend  # type: ignore
        except Exception as e:
            raise RuntimeError(
                "LLM_BACKEND=ollama selected, but Ollama backend could not be imported. "
                "Expected module: ai.llm.adapters.ollama_backend (OllamaBackend). "
                f"Import error: {e.__class__.__name__}: {e}"
            )
        return OllamaBackend()

    if backend in ("openai", "openai_backend"):
        # Only now do we import OpenAI adapter
        try:
            from ai.llm.adapters.openai_backend import OpenAIBackend  # type: ignore
        except Exception as e:
            raise RuntimeError(
                "LLM_BACKEND=openai selected, but OpenAI backend could not be imported. "
                "Expected module: ai.llm.adapters.openai_backend (OpenAIBackend). "
                f"Import error: {e.__class__.__name__}: {e}"
            )
        # Let OpenAIBackend enforce OPENAI_API_KEY if needed
        return OpenAIBackend()

    raise RuntimeError(
        f"Unknown LLM backend '{backend}'. Set LLM_BACKEND=ollama or LLM_BACKEND=openai."
    )