"""
Tests for the LLM backend factory — verifies that LLM_PROVIDER env var
routes to the correct backend class without making real API calls.
"""
import pytest
from unittest.mock import patch, MagicMock

from ai.llm.factory import create_backend


class TestFactoryRouting:
    def test_ollama_selected_by_default(self, monkeypatch):
        """No LLM_PROVIDER set → factory falls back to ollama."""
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        monkeypatch.delenv("LLM_BACKEND", raising=False)
        monkeypatch.delenv("AI_LLM_BACKEND", raising=False)
        backend = create_backend()
        assert type(backend).__name__ == "OllamaBackend"

    def test_ollama_selected_explicitly(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "ollama")
        backend = create_backend()
        assert type(backend).__name__ == "OllamaBackend"

    def test_legacy_llm_backend_var_respected(self, monkeypatch):
        """LLM_BACKEND (josh's naming) is honoured as a fallback."""
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        monkeypatch.setenv("LLM_BACKEND", "ollama")
        backend = create_backend()
        assert type(backend).__name__ == "OllamaBackend"

    def test_openai_selected(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
        with patch("ai.llm.adapters.openai_backend.OpenAI"):
            backend = create_backend()
        assert type(backend).__name__ == "OpenAIBackend"

    def test_gemini_selected(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "gemini")
        monkeypatch.setenv("GEMINI_API_KEY", "AIza-test-key")
        with patch("google.genai.Client"):
            backend = create_backend()
        assert type(backend).__name__ == "GeminiBackend"

    def test_unknown_provider_raises(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "gpt-turbo-9000")
        with pytest.raises(RuntimeError, match="Unknown LLM provider"):
            create_backend()

    def test_openai_missing_key_raises(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            with patch("ai.llm.adapters.openai_backend.OpenAI"):
                create_backend()

    def test_gemini_missing_key_raises(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "gemini")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
            with patch("google.genai.Client"):
                create_backend()
