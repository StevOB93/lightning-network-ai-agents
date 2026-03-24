# ai/llm/factory.py
from __future__ import annotations

# =============================================================================
# LLM backend factory — selects and instantiates the right adapter
#
# Two entry points:
#   create_backend()            — for legacy/agent use; picks a single backend
#   create_backend_for_role()   — for pipeline stages; allows per-stage config
#
# Selection order (both functions):
#   1. {ROLE}_LLM_BACKEND env var (only in create_backend_for_role)
#   2. LLM_BACKEND / AI_LLM_BACKEND env var
#   3. Default: "ollama"
#
# Per-stage model override (create_backend_for_role only):
#   {ROLE}_OLLAMA_MODEL  overrides OLLAMA_MODEL for that role
#   {ROLE}_OPENAI_MODEL  overrides OPENAI_MODEL for that role
#   {ROLE}_GEMINI_MODEL  overrides GEMINI_MODEL for that role
#   {ROLE}_CLAUDE_MODEL  overrides CLAUDE_MODEL for that role
#
# Deferred imports:
#   Adapter modules are imported only when selected. This means:
#   - If LLM_BACKEND=ollama, the openai/google-genai packages don't need to
#     be installed and won't cause an ImportError at startup.
#   - If an adapter is missing, a descriptive RuntimeError is raised at
#     call time, not at module import time.
# =============================================================================

import os


def _env(name: str, default: str | None = None) -> str | None:
    """Read and strip an env var, returning default if absent or blank."""
    v = os.getenv(name)
    if v is None:
        return default
    v = v.strip()
    return v if v else default


def create_backend():
    """
    Instantiate an LLM backend using the global LLM_BACKEND env var.

    Used by the legacy agent (ai.agent) which doesn't need per-stage config.
    For pipeline stages, use create_backend_for_role() instead.

    Supported LLM_BACKEND values:
      "ollama" / "local"   → OllamaBackend (local Ollama server)
      "openai"             → OpenAIBackend (requires OPENAI_API_KEY)
      "gemini"             → GeminiBackend (requires GEMINI_API_KEY)
      "claude"             → ClaudeBackend (requires ANTHROPIC_API_KEY)
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
        try:
            from ai.llm.adapters.openai_backend import OpenAIBackend  # type: ignore
        except Exception as e:
            raise RuntimeError(
                "LLM_BACKEND=openai selected, but OpenAI backend could not be imported. "
                "Expected module: ai.llm.adapters.openai_backend (OpenAIBackend). "
                f"Import error: {e.__class__.__name__}: {e}"
            )
        # OpenAIBackend enforces OPENAI_API_KEY internally and raises AuthError if missing
        return OpenAIBackend()

    if backend in ("gemini", "gemini_backend"):
        try:
            from ai.llm.adapters.gemini_backend import GeminiBackend  # type: ignore
        except Exception as e:
            raise RuntimeError(
                "LLM_BACKEND=gemini selected, but GeminiBackend could not be imported. "
                f"Import error: {e.__class__.__name__}: {e}"
            )
        return GeminiBackend()

    if backend in ("claude", "claude_backend", "anthropic"):
        try:
            from ai.llm.adapters.claude_backend import ClaudeBackend  # type: ignore
        except Exception as e:
            raise RuntimeError(
                "LLM_BACKEND=claude selected, but ClaudeBackend could not be imported. "
                "Expected module: ai.llm.adapters.claude_backend (ClaudeBackend). "
                f"Import error: {e.__class__.__name__}: {e}"
            )
        # ClaudeBackend enforces ANTHROPIC_API_KEY internally and raises AuthError if missing
        return ClaudeBackend()

    raise RuntimeError(
        f"Unknown LLM backend '{backend}'. Set LLM_BACKEND=ollama, openai, gemini, or claude."
    )


def create_backend_for_role(role: str):
    """
    Instantiate an LLM backend for a specific pipeline role.

    Allows each stage (translator, planner, executor, summarizer) to use a
    different model or backend. For example, the translator might use a cheaper
    model for NL parsing while the planner uses a stronger model for reasoning.

    Role names are uppercased and hyphenated with underscores:
      "translator" → TRANSLATOR_LLM_BACKEND, TRANSLATOR_OLLAMA_MODEL, etc.

    Env var resolution order for the backend type:
      1. {ROLE}_LLM_BACKEND (e.g. TRANSLATOR_LLM_BACKEND=gemini)
      2. LLM_BACKEND / AI_LLM_BACKEND (global fallback)
      3. "ollama" (hardcoded default)

    Env var resolution order for the model name (Ollama example):
      1. TRANSLATOR_OLLAMA_MODEL (role-specific override)
      2. OLLAMA_MODEL (global model)
      3. OllamaBackend's hardcoded default ("llama3.2:3b")
    """
    role_upper = role.upper().replace("-", "_")
    backend = (
        _env(f"{role_upper}_LLM_BACKEND")
        or _env("LLM_BACKEND")
        or _env("AI_LLM_BACKEND")
        or "ollama"
    ).lower()

    if backend in ("ollama", "local", "ollama_backend"):
        try:
            from ai.llm.adapters.ollama_backend import OllamaBackend  # type: ignore
        except Exception as e:
            raise RuntimeError(
                f"LLM backend for role '{role}' is ollama, but OllamaBackend could not be imported. "
                f"Import error: {e.__class__.__name__}: {e}"
            )
        model = _env(f"{role_upper}_OLLAMA_MODEL") or _env("OLLAMA_MODEL") or None
        return OllamaBackend(model=model)

    if backend in ("openai", "openai_backend"):
        try:
            from ai.llm.adapters.openai_backend import OpenAIBackend  # type: ignore
        except Exception as e:
            raise RuntimeError(
                f"LLM backend for role '{role}' is openai, but OpenAIBackend could not be imported. "
                f"Import error: {e.__class__.__name__}: {e}"
            )
        model = _env(f"{role_upper}_OPENAI_MODEL") or _env("OPENAI_MODEL") or None
        return OpenAIBackend(model=model)

    if backend in ("gemini", "gemini_backend"):
        try:
            from ai.llm.adapters.gemini_backend import GeminiBackend  # type: ignore
        except Exception as e:
            raise RuntimeError(
                f"LLM backend for role '{role}' is gemini, but GeminiBackend could not be imported. "
                f"Import error: {e.__class__.__name__}: {e}"
            )
        model = _env(f"{role_upper}_GEMINI_MODEL") or _env("GEMINI_MODEL") or None
        return GeminiBackend(model=model)

    if backend in ("claude", "claude_backend", "anthropic"):
        try:
            from ai.llm.adapters.claude_backend import ClaudeBackend  # type: ignore
        except Exception as e:
            raise RuntimeError(
                f"LLM backend for role '{role}' is claude, but ClaudeBackend could not be imported. "
                f"Import error: {e.__class__.__name__}: {e}"
            )
        model = _env(f"{role_upper}_CLAUDE_MODEL") or _env("CLAUDE_MODEL") or None
        return ClaudeBackend(model=model)

    raise RuntimeError(
        f"Unknown LLM backend '{backend}' for role '{role}'. "
        "Set LLM_BACKEND=ollama, openai, gemini, or claude."
    )
