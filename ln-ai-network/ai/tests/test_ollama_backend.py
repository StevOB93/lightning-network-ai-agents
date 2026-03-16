"""
Tests for OllamaBackend — exercises step() with mocked HTTP responses.
No live Ollama server required.
"""
import json
import pytest
import requests as req_lib
from unittest.mock import patch, MagicMock

from ai.llm.adapters.ollama_backend import OllamaBackend
from ai.llm.base import LLMRequest, LLMResponse, ToolCall, TransientAPIError, PermanentAPIError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TOOL_SCHEMA = [
    {"type": "function", "function": {"name": "network_health", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "ln_getinfo", "parameters": {"type": "object", "properties": {"node": {"type": "integer"}}}}},
]


def _backend() -> OllamaBackend:
    return OllamaBackend(base_url="http://localhost:11434", model="test-model")


def _request(tools=None, messages=None) -> LLMRequest:
    return LLMRequest(
        messages=messages or [{"role": "user", "content": "check network"}],
        tools=tools if tools is not None else TOOL_SCHEMA,
        max_output_tokens=64,
        temperature=0.0,
    )


def _mock_http(status: int = 200, body: dict = None) -> MagicMock:
    mock = MagicMock()
    mock.status_code = status
    body = body or {}
    mock.json.return_value = body
    mock.text = json.dumps(body)
    return mock


# ---------------------------------------------------------------------------
# Structured tool_call responses
# ---------------------------------------------------------------------------

class TestStructuredToolCall:
    def test_single_tool_call_parsed(self):
        body = {
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"function": {"name": "network_health", "arguments": {}}}],
            },
            "prompt_eval_count": 12,
            "eval_count": 4,
        }
        with patch("requests.post", return_value=_mock_http(body=body)):
            resp = _backend().step(_request())

        assert isinstance(resp, LLMResponse)
        assert resp.type == "tool_call"
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].name == "network_health"
        assert resp.tool_calls[0].args == {}

    def test_tool_call_with_args(self):
        body = {
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"function": {"name": "ln_getinfo", "arguments": {"node": 1}}}],
            },
        }
        with patch("requests.post", return_value=_mock_http(body=body)):
            resp = _backend().step(_request())

        assert resp.type == "tool_call"
        assert resp.tool_calls[0].name == "ln_getinfo"
        assert resp.tool_calls[0].args == {"node": 1}

    def test_string_arguments_parsed_as_json(self):
        """Ollama sometimes serializes arguments as a JSON string."""
        body = {
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"function": {"name": "ln_getinfo", "arguments": '{"node": 2}'}}],
            },
        }
        with patch("requests.post", return_value=_mock_http(body=body)):
            resp = _backend().step(_request())

        assert resp.tool_calls[0].args == {"node": 2}


# ---------------------------------------------------------------------------
# Usage tracking
# ---------------------------------------------------------------------------

class TestUsageTracking:
    def test_usage_populated_when_counts_present(self):
        body = {
            "message": {"role": "assistant", "content": None,
                         "tool_calls": [{"function": {"name": "network_health", "arguments": {}}}]},
            "prompt_eval_count": 20,
            "eval_count": 8,
        }
        with patch("requests.post", return_value=_mock_http(body=body)):
            resp = _backend().step(_request())

        assert resp.usage is not None
        assert resp.usage.prompt_tokens == 20
        assert resp.usage.output_tokens == 8
        assert resp.usage.total_tokens == 28

    def test_usage_none_when_counts_absent(self):
        body = {"message": {"role": "assistant", "content": "ok"}}
        with patch("requests.post", return_value=_mock_http(body=body)):
            resp = _backend().step(_request(tools=[]))

        assert resp.usage is None


# ---------------------------------------------------------------------------
# Final response
# ---------------------------------------------------------------------------

class TestFinalResponse:
    def test_content_returned_as_final(self):
        body = {"message": {"role": "assistant", "content": "All nodes healthy."}}
        with patch("requests.post", return_value=_mock_http(body=body)):
            resp = _backend().step(_request(tools=[]))

        assert resp.type == "final"
        assert resp.content == "All nodes healthy."
        assert resp.tool_calls == []

    def test_empty_content_becomes_empty_string(self):
        body = {"message": {"role": "assistant", "content": None}}
        with patch("requests.post", return_value=_mock_http(body=body)):
            resp = _backend().step(_request(tools=[]))

        assert resp.type == "final"
        assert resp.content == ""


# ---------------------------------------------------------------------------
# Fallback text parsing (Ollama fails to emit structured tool_calls)
# ---------------------------------------------------------------------------

class TestFallbackParsing:
    def test_json_envelope_parsed(self):
        text = '{"tool": "network_health", "args": {}}'
        body = {"message": {"role": "assistant", "content": text}}
        with patch("requests.post", return_value=_mock_http(body=body)):
            resp = _backend().step(_request())

        assert resp.type == "tool_call"
        assert resp.tool_calls[0].name == "network_health"

    def test_function_call_syntax_parsed(self):
        text = "network_health()"
        body = {"message": {"role": "assistant", "content": text}}
        with patch("requests.post", return_value=_mock_http(body=body)):
            resp = _backend().step(_request())

        assert resp.type == "tool_call"
        assert resp.tool_calls[0].name == "network_health"

    def test_unknown_tool_name_falls_through_to_final(self):
        """Fallback only fires for tools in the schema — unknown names stay as final."""
        text = '{"tool": "drop_database", "args": {}}'
        body = {"message": {"role": "assistant", "content": text}}
        with patch("requests.post", return_value=_mock_http(body=body)):
            resp = _backend().step(_request())

        assert resp.type == "final"

    def test_no_tools_in_schema_prevents_fallback(self):
        text = '{"tool": "network_health", "args": {}}'
        body = {"message": {"role": "assistant", "content": text}}
        with patch("requests.post", return_value=_mock_http(body=body)):
            resp = _backend().step(_request(tools=[]))

        assert resp.type == "final"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def test_5xx_raises_transient(self):
        with patch("requests.post", return_value=_mock_http(status=503)):
            with pytest.raises(TransientAPIError):
                _backend().step(_request())

    def test_4xx_raises_permanent(self):
        with patch("requests.post", return_value=_mock_http(status=400)):
            with pytest.raises(PermanentAPIError):
                _backend().step(_request())

    def test_connection_error_raises_transient(self):
        with patch("requests.post", side_effect=req_lib.ConnectionError("refused")):
            with pytest.raises(TransientAPIError):
                _backend().step(_request())

    def test_timeout_raises_transient(self):
        with patch("requests.post", side_effect=req_lib.Timeout("timed out")):
            with pytest.raises(TransientAPIError):
                _backend().step(_request())
