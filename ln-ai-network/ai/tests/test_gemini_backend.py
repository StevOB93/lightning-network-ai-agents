"""
Tests for GeminiBackend — uses a patched google.genai.Client so no real
API calls are made. Also documents the run_prompt() contract.
"""
import pytest
from unittest.mock import patch, MagicMock

from ai.llm.adapters.gemini_backend import GeminiBackend


def _make_backend(monkeypatch) -> GeminiBackend:
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    with patch("google.genai.Client"):
        return GeminiBackend()


class TestGeminiBackendContract:
    def test_step_raises_runtime_error(self, monkeypatch):
        """step() violates LLMBackend contract — documents the known issue."""
        backend = _make_backend(monkeypatch)
        with pytest.raises(RuntimeError, match="run_prompt"):
            backend.step([], [])

    def test_run_prompt_calls_generate_content(self, monkeypatch):
        backend = _make_backend(monkeypatch)

        mock_response = MagicMock()
        mock_response.text = "All three nodes are online."
        backend.client.models.generate_content.return_value = mock_response

        def fake_tool():
            """A dummy tool."""
            return {"status": "ok"}

        result = backend.run_prompt(
            system_prompt="You are a Lightning Network agent.",
            user_text="Check network health.",
            tool_functions=[fake_tool],
        )

        assert result["type"] == "final"
        assert "online" in result["content"]
        backend.client.models.generate_content.assert_called_once()

    def test_run_prompt_uses_correct_model(self, monkeypatch):
        backend = _make_backend(monkeypatch)
        mock_response = MagicMock()
        mock_response.text = "ok"
        backend.client.models.generate_content.return_value = mock_response

        backend.run_prompt(system_prompt="sys", user_text="go", tool_functions=[])

        call_kwargs = backend.client.models.generate_content.call_args[1]
        assert call_kwargs["model"] == "gemini-2.5-flash"

    def test_run_prompt_empty_text_falls_back_to_dump(self, monkeypatch):
        """When response.text is falsy, backend dumps the full response as JSON."""
        backend = _make_backend(monkeypatch)
        mock_response = MagicMock()
        mock_response.text = ""
        mock_response.model_dump.return_value = {"candidates": []}
        backend.client.models.generate_content.return_value = mock_response

        result = backend.run_prompt(system_prompt="s", user_text="u", tool_functions=[])

        assert result["type"] == "final"
        assert "candidates" in result["content"]


class TestGeminiBackendInit:
    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
            with patch("google.genai.Client"):
                GeminiBackend()

    def test_custom_model_from_env(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")
        monkeypatch.setenv("GEMINI_MODEL", "gemini-1.5-pro")
        with patch("google.genai.Client"):
            backend = GeminiBackend()
        assert backend.model == "gemini-1.5-pro"
