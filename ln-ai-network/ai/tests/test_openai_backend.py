"""
Tests for OpenAIBackend — exercises step() with a mocked OpenAI SDK client.
No real API calls are made.

Note: OpenAIBackend.step() currently takes (messages, tools) dicts rather than
LLMRequest and returns a dict rather than LLMResponse — this is a known interface
contract violation (see analysis in CLAUDE.md). Tests verify the actual
current behaviour so regressions are caught when the contract is fixed.
"""
import json
import pytest
from unittest.mock import patch, MagicMock

from ai.llm.adapters.openai_backend import OpenAIBackend

TOOLS = [{"type": "function", "function": {"name": "network_health", "parameters": {}}}]
MESSAGES = [{"role": "user", "content": "check health"}]


def _make_backend(monkeypatch) -> OpenAIBackend:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o")
    with patch("ai.llm.adapters.openai_backend.OpenAI"):
        return OpenAIBackend()


def _tool_call_choice(name: str, args: str = "{}") -> MagicMock:
    tool_call = MagicMock()
    tool_call.function.name = name
    tool_call.function.arguments = args
    choice = MagicMock()
    choice.finish_reason = "tool_calls"
    choice.message.tool_calls = [tool_call]
    choice.message.content = None
    return choice


def _final_choice(content: str) -> MagicMock:
    choice = MagicMock()
    choice.finish_reason = "stop"
    choice.message.content = content
    choice.message.tool_calls = None
    return choice


class TestOpenAIBackendStep:
    def test_tool_call_response(self, monkeypatch):
        backend = _make_backend(monkeypatch)
        resp_mock = MagicMock()
        resp_mock.choices = [_tool_call_choice("network_health", "{}")]
        backend.client.chat.completions.create.return_value = resp_mock

        result = backend.step(MESSAGES, TOOLS)

        assert result["type"] == "tool_call"
        assert result["tool_name"] == "network_health"
        assert result["tool_args"] == {}

    def test_tool_call_with_args(self, monkeypatch):
        backend = _make_backend(monkeypatch)
        resp_mock = MagicMock()
        resp_mock.choices = [_tool_call_choice("ln_getinfo", '{"node": 1}')]
        backend.client.chat.completions.create.return_value = resp_mock

        result = backend.step(MESSAGES, TOOLS)

        assert result["tool_name"] == "ln_getinfo"
        assert result["tool_args"] == {"node": 1}

    def test_final_response(self, monkeypatch):
        backend = _make_backend(monkeypatch)
        resp_mock = MagicMock()
        resp_mock.choices = [_final_choice("Network looks healthy.")]
        backend.client.chat.completions.create.return_value = resp_mock

        result = backend.step(MESSAGES, [])

        assert result["type"] == "final"
        assert result["content"] == "Network looks healthy."
        assert result["tool_name"] is None

    def test_model_passed_to_api(self, monkeypatch):
        backend = _make_backend(monkeypatch)
        resp_mock = MagicMock()
        resp_mock.choices = [_final_choice("ok")]
        backend.client.chat.completions.create.return_value = resp_mock

        backend.step(MESSAGES, [])

        call_kwargs = backend.client.chat.completions.create.call_args[1]
        assert call_kwargs["model"] == "gpt-4o"


class TestOpenAIBackendInit:
    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            with patch("ai.llm.adapters.openai_backend.OpenAI"):
                OpenAIBackend()

    def test_custom_model_from_env(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("OPENAI_MODEL", "gpt-4-turbo")
        with patch("ai.llm.adapters.openai_backend.OpenAI"):
            backend = OpenAIBackend()
        assert backend.model == "gpt-4-turbo"
