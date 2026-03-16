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
      1) LLM_PROVIDER env var (preferred, matches .env.example)
      2) LLM_BACKEND / AI_LLM_BACKEND env var (legacy aliases)
      3) default to 'ollama'

    Supported values:
      - 'ollama'   — local Ollama (default; no API key required)
      - 'openai'   — OpenAI API (requires OPENAI_API_KEY)
      - 'gemini'   — Google Gemini native SDK (requires GEMINI_API_KEY)

    Each backend is lazily imported so a missing SDK only errors when
    that provider is actually selected.
    """
    backend = (
        _env("LLM_PROVIDER")
        or _env("LLM_BACKEND")
        or _env("AI_LLM_BACKEND")
        or "ollama"
    ).lower()

    if backend in ("ollama", "local", "ollama_backend"):
        try:
            from ai.llm.adapters.ollama_backend import OllamaBackend  # type: ignore
        except Exception as e:
            raise RuntimeError(
                "LLM_PROVIDER=ollama selected, but Ollama backend could not be imported. "
                "Expected module: ai.llm.adapters.ollama_backend (OllamaBackend). "
                f"Import error: {e.__class__.__name__}: {e}"
            )
        return OllamaBackend()

    if backend in ("openai", "openai_backend"):
        try:
            from ai.llm.adapters.openai_backend import OpenAIBackend  # type: ignore
        except Exception as e:
            raise RuntimeError(
                "LLM_PROVIDER=openai selected, but OpenAI backend could not be imported. "
                "Expected module: ai.llm.adapters.openai_backend (OpenAIBackend). "
                f"Import error: {e.__class__.__name__}: {e}"
            )
        return OpenAIBackend()

    if backend in ("gemini", "gemini_backend"):
        try:
            from ai.llm.adapters.gemini_backend import GeminiBackend  # type: ignore
        except Exception as e:
            raise RuntimeError(
                "LLM_PROVIDER=gemini selected, but Gemini backend could not be imported. "
                "Ensure 'google-genai' is installed: pip install -r requirements.txt. "
                f"Import error: {e.__class__.__name__}: {e}"
            )
        return GeminiBackend()

    raise RuntimeError(
        f"Unknown LLM provider '{backend}'. Set LLM_PROVIDER=ollama, openai, or gemini."
    )