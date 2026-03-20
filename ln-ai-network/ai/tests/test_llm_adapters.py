"""Tests for LLM backend adapters: OpenAI, Gemini, and Ollama.

Strategy:
  - All external HTTP/SDK calls are patched with unittest.mock.
  - No real API keys or network access are required.
  - Tests verify the normalized LLMResponse output and the error taxonomy
    (that the right LLMError subclass is raised for each HTTP status code).
  - Each test is self-contained: patches are applied per-test to avoid
    state leaking across tests.

OpenAI test coverage:
  - Successful tool call response → LLMResponse(type="tool_call")
  - Successful text response → LLMResponse(type="final")
  - 401 auth error → AuthError
  - 429 rate limit → RateLimitError
  - 500 server error → TransientAPIError
  - Unknown error → PermanentAPIError
  - _parse_args(): None, dict, valid JSON str, invalid JSON str

Gemini test coverage:
  - Successful tool call response → LLMResponse(type="tool_call")
  - Successful text response → LLMResponse(type="final")
  - Empty candidates → LLMResponse(type="final", content="")
  - 401 auth error → AuthError
  - 429 rate limit → RateLimitError
  - 500 server error → TransientAPIError
  - Unknown error → PermanentAPIError
  - Message format conversion: system, user, assistant, tool roles
  - Schema conversion: string, integer, object, array, enum types
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ai.llm.base import (
    AuthError,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    PermanentAPIError,
    RateLimitError,
    ToolCall,
    TransientAPIError,
)


# =============================================================================
# Helpers
# =============================================================================

def _make_request(tools: list | None = None, messages: list | None = None) -> LLMRequest:
    """Build a minimal LLMRequest for use in tests."""
    return LLMRequest(
        messages=messages or [{"role": "user", "content": "hi"}],
        tools=tools or [],
        max_output_tokens=256,
        temperature=0.1,
    )


# =============================================================================
# OpenAI backend tests
# =============================================================================

class TestOpenAIBackend:
    """Tests for ai.llm.adapters.openai_backend.OpenAIBackend."""

    # ---------- _parse_args --------------------------------------------------

    def test_parse_args_none(self):
        from ai.llm.adapters.openai_backend import _parse_args
        assert _parse_args(None) == {}

    def test_parse_args_dict(self):
        from ai.llm.adapters.openai_backend import _parse_args
        assert _parse_args({"node": 1}) == {"node": 1}

    def test_parse_args_valid_json_str(self):
        from ai.llm.adapters.openai_backend import _parse_args
        assert _parse_args('{"node": 2, "amount": 500}') == {"node": 2, "amount": 500}

    def test_parse_args_invalid_json_str(self):
        from ai.llm.adapters.openai_backend import _parse_args
        assert _parse_args("not json") == {}

    def test_parse_args_json_non_dict(self):
        from ai.llm.adapters.openai_backend import _parse_args
        # JSON that is valid but not a dict → {}
        assert _parse_args("[1, 2, 3]") == {}

    # ---------- Constructing the backend ------------------------------------

    def _make_backend(self):
        """Instantiate OpenAIBackend with a mocked OpenAI client."""
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
            with patch("ai.llm.adapters.openai_backend.OpenAI") as MockOpenAI:
                from ai.llm.adapters.openai_backend import OpenAIBackend
                backend = OpenAIBackend()
                backend.client = MockOpenAI.return_value
                return backend

    # ---------- Successful tool call -----------------------------------------

    def test_step_tool_call(self):
        backend = self._make_backend()

        # Build the mock SDK response for a tool call
        tc = SimpleNamespace(
            function=SimpleNamespace(
                name="ln_getinfo",
                arguments='{"node": 1}',
            )
        )
        choice = SimpleNamespace(
            finish_reason="tool_calls",
            message=SimpleNamespace(content=None, tool_calls=[tc]),
        )
        usage = SimpleNamespace(prompt_tokens=10, completion_tokens=20, total_tokens=30)
        mock_resp = SimpleNamespace(choices=[choice], usage=usage)
        backend.client.chat.completions.create.return_value = mock_resp

        result = backend.step(_make_request(tools=[{"type": "function", "function": {"name": "ln_getinfo"}}]))

        assert result.type == "tool_call"
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "ln_getinfo"
        assert result.tool_calls[0].args == {"node": 1}
        assert result.usage == LLMUsage(10, 20, 30)

    # ---------- Successful text response -------------------------------------

    def test_step_final_text(self):
        backend = self._make_backend()

        choice = SimpleNamespace(
            finish_reason="stop",
            message=SimpleNamespace(content="Hello!", tool_calls=None),
        )
        mock_resp = SimpleNamespace(choices=[choice], usage=None)
        backend.client.chat.completions.create.return_value = mock_resp

        result = backend.step(_make_request())

        assert result.type == "final"
        assert result.content == "Hello!"
        assert result.tool_calls == []
        assert result.usage is None

    # ---------- Error mapping ------------------------------------------------

    def _raise_side_effect(self, msg: str):
        def _raise(*args, **kwargs):
            raise Exception(msg)
        return _raise

    def test_auth_error(self):
        backend = self._make_backend()
        backend.client.chat.completions.create.side_effect = self._raise_side_effect("401 authentication failed")
        with pytest.raises(AuthError):
            backend.step(_make_request())

    def test_rate_limit_error(self):
        backend = self._make_backend()
        backend.client.chat.completions.create.side_effect = self._raise_side_effect("429 rate_limit exceeded")
        with pytest.raises(RateLimitError):
            backend.step(_make_request())

    def test_transient_error_500(self):
        backend = self._make_backend()
        backend.client.chat.completions.create.side_effect = self._raise_side_effect("500 internal server error")
        with pytest.raises(TransientAPIError):
            backend.step(_make_request())

    def test_transient_error_timeout(self):
        backend = self._make_backend()
        backend.client.chat.completions.create.side_effect = self._raise_side_effect("timeout connecting to server")
        with pytest.raises(TransientAPIError):
            backend.step(_make_request())

    def test_permanent_error_unknown(self):
        backend = self._make_backend()
        backend.client.chat.completions.create.side_effect = self._raise_side_effect("bad request: unknown model")
        with pytest.raises(PermanentAPIError):
            backend.step(_make_request())

    # ---------- No usage field -----------------------------------------------

    def test_step_no_usage(self):
        backend = self._make_backend()
        choice = SimpleNamespace(
            finish_reason="stop",
            message=SimpleNamespace(content="ok", tool_calls=None),
        )
        mock_resp = SimpleNamespace(choices=[choice], usage=None)
        backend.client.chat.completions.create.return_value = mock_resp

        result = backend.step(_make_request())
        assert result.usage is None

    # ---------- Tools omitted when empty ------------------------------------

    def test_tools_not_passed_when_empty(self):
        backend = self._make_backend()
        choice = SimpleNamespace(
            finish_reason="stop",
            message=SimpleNamespace(content="ok", tool_calls=None),
        )
        mock_resp = SimpleNamespace(choices=[choice], usage=None)
        backend.client.chat.completions.create.return_value = mock_resp

        backend.step(_make_request(tools=[]))

        call_kwargs = backend.client.chat.completions.create.call_args[1]
        assert "tools" not in call_kwargs

    def test_tools_passed_when_non_empty(self):
        backend = self._make_backend()
        tc = SimpleNamespace(
            function=SimpleNamespace(name="ln_listfunds", arguments="{}")
        )
        choice = SimpleNamespace(
            finish_reason="tool_calls",
            message=SimpleNamespace(content=None, tool_calls=[tc]),
        )
        mock_resp = SimpleNamespace(choices=[choice], usage=None)
        backend.client.chat.completions.create.return_value = mock_resp

        tools = [{"type": "function", "function": {"name": "ln_listfunds"}}]
        backend.step(_make_request(tools=tools))

        call_kwargs = backend.client.chat.completions.create.call_args[1]
        assert "tools" in call_kwargs


# =============================================================================
# Gemini backend tests
# =============================================================================

class TestGeminiBackend:
    """Tests for ai.llm.adapters.gemini_backend.GeminiBackend.

    The google-genai package may not be installed in the test environment.
    setup_method injects fake modules into sys.modules for the duration of
    each test so that lazy `from google.genai import types` calls inside
    the production code resolve against our mocks rather than the real SDK.
    """

    def setup_method(self, method):
        import sys
        import importlib

        # Build a minimal fake google.genai module tree
        self.fake_types = MagicMock()
        self.fake_types.Schema = MagicMock(side_effect=lambda **kw: SimpleNamespace(**kw))
        self.fake_types.FunctionDeclaration = MagicMock(side_effect=lambda **kw: SimpleNamespace(**kw))
        self.fake_types.Tool = MagicMock(side_effect=lambda **kw: SimpleNamespace(**kw))
        self.fake_types.Content = MagicMock(side_effect=lambda **kw: SimpleNamespace(**kw))
        self.fake_types.Part = MagicMock()
        self.fake_types.Part.return_value = SimpleNamespace(text="", function_call=None)
        self.fake_types.FunctionCall = MagicMock(side_effect=lambda **kw: SimpleNamespace(**kw))
        self.fake_types.FunctionResponse = MagicMock(side_effect=lambda **kw: SimpleNamespace(**kw))
        self.fake_types.GenerateContentConfig = MagicMock(side_effect=lambda **kw: SimpleNamespace(**kw))
        self.fake_types.AutomaticFunctionCallingConfig = MagicMock(side_effect=lambda **kw: SimpleNamespace(**kw))
        self.fake_types.HttpOptions = MagicMock(side_effect=lambda **kw: SimpleNamespace(**kw))

        self.fake_genai = MagicMock()
        self.fake_genai.types = self.fake_types
        self.fake_client = MagicMock()
        self.fake_genai.Client.return_value = self.fake_client

        fake_google = MagicMock()
        fake_google.genai = self.fake_genai

        # Start patches — kept alive until teardown_method
        self._modules_patcher = patch.dict(sys.modules, {
            "google": fake_google,
            "google.genai": self.fake_genai,
            "google.genai.types": self.fake_types,
        })
        self._env_patcher = patch.dict("os.environ", {"GEMINI_API_KEY": "test-key"})
        self._modules_patcher.start()
        self._env_patcher.start()

        # Reload so that module-level code inside gemini_backend sees the mocks
        import ai.llm.adapters.gemini_backend as gmod
        importlib.reload(gmod)
        self._gmod = gmod

        self.backend = gmod.GeminiBackend.__new__(gmod.GeminiBackend)
        self.backend._genai = self.fake_genai
        self.backend._types = self.fake_types
        self.backend.client = self.fake_client
        self.backend.model = "gemini-2.5-flash"

    def teardown_method(self, method):
        self._env_patcher.stop()
        self._modules_patcher.stop()

    # ---------- _parse_response: tool call -----------------------------------

    def test_parse_response_tool_call(self):
        # Build a fake Gemini response with a function_call part
        fc = SimpleNamespace(name="ln_getinfo", args={"node": 1})
        part = SimpleNamespace(function_call=fc, text=None)
        candidate = SimpleNamespace(content=SimpleNamespace(parts=[part]))
        usage_meta = SimpleNamespace(
            prompt_token_count=5, candidates_token_count=10, total_token_count=15
        )
        response = SimpleNamespace(candidates=[candidate], usage_metadata=usage_meta)

        result = self.backend._parse_response(response)

        assert result.type == "tool_call"
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "ln_getinfo"
        assert result.tool_calls[0].args == {"node": 1}
        assert result.usage == LLMUsage(5, 10, 15)

    # ---------- _parse_response: text response --------------------------------

    def test_parse_response_final_text(self):
        part = SimpleNamespace(function_call=None, text="Node 1 is running.")
        candidate = SimpleNamespace(content=SimpleNamespace(parts=[part]))
        usage_meta = SimpleNamespace(
            prompt_token_count=3, candidates_token_count=8, total_token_count=11
        )
        response = SimpleNamespace(candidates=[candidate], usage_metadata=usage_meta)

        result = self.backend._parse_response(response)

        assert result.type == "final"
        assert result.content == "Node 1 is running."
        assert result.tool_calls == []

    # ---------- _parse_response: empty candidates ----------------------------

    def test_parse_response_empty_candidates(self):
        usage_meta = SimpleNamespace(
            prompt_token_count=0, candidates_token_count=0, total_token_count=0
        )
        response = SimpleNamespace(candidates=[], usage_metadata=usage_meta)

        result = self.backend._parse_response(response)

        assert result.type == "final"
        assert result.content == ""
        assert result.tool_calls == []

    # ---------- Error mapping ------------------------------------------------

    def test_auth_error(self):
        self.fake_client.models.generate_content.side_effect = Exception("401 invalid api key")
        with pytest.raises(AuthError):
            self.backend.step(_make_request())

    def test_rate_limit_error(self):
        self.fake_client.models.generate_content.side_effect = Exception("429 quota exceeded")
        with pytest.raises(RateLimitError):
            self.backend.step(_make_request())

    def test_transient_error_500(self):
        self.fake_client.models.generate_content.side_effect = Exception("500 internal server error")
        with pytest.raises(TransientAPIError):
            self.backend.step(_make_request())

    def test_permanent_error_unknown(self):
        self.fake_client.models.generate_content.side_effect = Exception("invalid argument: context too long")
        with pytest.raises(PermanentAPIError):
            self.backend.step(_make_request())

    # ---------- Schema conversion --------------------------------------------

    def test_convert_schema_string(self):
        """String type maps to STRING."""
        result = self._gmod._convert_schema({"type": "string", "description": "A node ID"})
        assert result.type == "STRING"
        assert result.description == "A node ID"

    def test_convert_schema_integer(self):
        result = self._gmod._convert_schema({"type": "integer"})
        assert result.type == "INTEGER"

    def test_convert_schema_object_with_properties(self):
        schema = {
            "type": "object",
            "properties": {
                "node": {"type": "integer"},
                "label": {"type": "string"},
            },
            "required": ["node"],
        }
        result = self._gmod._convert_schema(schema)
        assert result.type == "OBJECT"
        assert "node" in result.properties
        assert "label" in result.properties
        assert result.required == ["node"]

    def test_convert_schema_array(self):
        schema = {"type": "array", "items": {"type": "string"}}
        result = self._gmod._convert_schema(schema)
        assert result.type == "ARRAY"
        assert result.items.type == "STRING"

    def test_convert_schema_enum(self):
        schema = {"type": "string", "enum": ["abort", "retry", "skip"]}
        result = self._gmod._convert_schema(schema)
        assert result.enum == ["abort", "retry", "skip"]

    def test_convert_schema_unknown_type_defaults_to_string(self):
        result = self._gmod._convert_schema({"type": "invalid_type"})
        assert result.type == "STRING"

    # ---------- Message format conversion ------------------------------------

    def test_message_conversion_system_extracted(self):
        """System messages are extracted as system_instruction, not in contents."""
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello"},
        ]
        system_instruction, contents = self._gmod._openai_messages_to_gemini(messages)
        assert system_instruction == "You are a helpful assistant."
        assert len(contents) == 1

    def test_message_conversion_no_system(self):
        """No system message → empty system_instruction."""
        messages = [{"role": "user", "content": "Hello"}]
        system_instruction, contents = self._gmod._openai_messages_to_gemini(messages)
        assert system_instruction == ""
        assert len(contents) == 1

    def test_message_conversion_tool_role(self):
        """Tool result messages → user-role function_response parts."""
        messages = [
            {"role": "user", "content": "call a tool"},
            {"role": "tool", "name": "ln_getinfo", "content": '{"id": "abc123"}'},
        ]
        system_instruction, contents = self._gmod._openai_messages_to_gemini(messages)
        # user message + tool result (also user-role in Gemini)
        assert len(contents) == 2


# =============================================================================
# Ollama backend tests
# =============================================================================

class TestOllamaBackend:
    """Tests for ai.llm.adapters.ollama_backend.OllamaBackend.

    All HTTP calls are patched via unittest.mock.patch so no real Ollama
    server is required.
    """

    def _make_backend(self):
        from ai.llm.adapters.ollama_backend import OllamaBackend
        return OllamaBackend(base_url="http://fake-ollama:11434", model="test-model")

    def _mock_response(self, status: int, body: Any) -> MagicMock:
        """Build a fake requests.Response."""
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = body
        resp.text = str(body)
        return resp

    # ---------- Structured tool_calls in response ----------------------------

    def test_structured_tool_call(self):
        """Ollama returns structured tool_calls → LLMResponse(type='tool_call')."""
        backend = self._make_backend()
        body = {
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"function": {"name": "ln_getinfo", "arguments": {"node": 1}}}
                ],
            },
            "prompt_eval_count": 10,
            "eval_count": 5,
        }
        with patch("requests.post", return_value=self._mock_response(200, body)):
            result = backend.step(_make_request())
        assert result.type == "tool_call"
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "ln_getinfo"
        assert result.tool_calls[0].args == {"node": 1}
        assert result.usage.prompt_tokens == 10
        assert result.usage.output_tokens == 5

    def test_structured_tool_call_args_as_json_string(self):
        """arguments as a JSON string (some Ollama versions) are decoded."""
        backend = self._make_backend()
        body = {
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"function": {"name": "ln_listfunds", "arguments": '{"node": 2}'}}
                ],
            },
        }
        with patch("requests.post", return_value=self._mock_response(200, body)):
            result = backend.step(_make_request())
        assert result.type == "tool_call"
        assert result.tool_calls[0].args == {"node": 2}

    # ---------- Fallback text tool call parsing ------------------------------

    def test_text_fallback_json_object_form(self):
        """Content with JSON {"tool": ..., "args": {...}} is parsed as tool call."""
        from ai.llm.adapters.ollama_backend import OllamaBackend
        backend = self._make_backend()
        body = {
            "message": {
                "role": "assistant",
                "content": '{"tool": "ln_getinfo", "args": {"node": 1}}',
                "tool_calls": [],
            },
        }
        tools = [{"type": "function", "function": {"name": "ln_getinfo"}}]
        with patch("requests.post", return_value=self._mock_response(200, body)):
            result = backend.step(_make_request(tools=tools))
        assert result.type == "tool_call"
        assert result.tool_calls[0].name == "ln_getinfo"

    def test_text_fallback_unknown_tool_name_is_final(self):
        """Text that parses as a tool call but name not in schema → final response."""
        backend = self._make_backend()
        body = {
            "message": {
                "role": "assistant",
                "content": '{"tool": "hallucinated_tool", "args": {}}',
                "tool_calls": [],
            },
        }
        tools = [{"type": "function", "function": {"name": "ln_getinfo"}}]
        with patch("requests.post", return_value=self._mock_response(200, body)):
            result = backend.step(_make_request(tools=tools))
        assert result.type == "final"

    # ---------- Plain text final response ------------------------------------

    def test_plain_text_response(self):
        """No tool_calls and content not parseable as tool → final text response."""
        backend = self._make_backend()
        body = {"message": {"role": "assistant", "content": "Node 1 is online."}}
        with patch("requests.post", return_value=self._mock_response(200, body)):
            result = backend.step(_make_request())
        assert result.type == "final"
        assert result.content == "Node 1 is online."

    def test_missing_usage_fields_returns_none_usage(self):
        """Older Ollama versions omit eval_count fields → usage is None."""
        backend = self._make_backend()
        body = {"message": {"role": "assistant", "content": "hi"}}
        with patch("requests.post", return_value=self._mock_response(200, body)):
            result = backend.step(_make_request())
        assert result.usage is None

    # ---------- Error mapping ------------------------------------------------

    def test_5xx_raises_transient_error(self):
        backend = self._make_backend()
        with patch("requests.post", return_value=self._mock_response(503, "unavailable")):
            with pytest.raises(TransientAPIError):
                backend.step(_make_request())

    def test_4xx_raises_permanent_error(self):
        backend = self._make_backend()
        with patch("requests.post", return_value=self._mock_response(400, "bad request")):
            with pytest.raises(PermanentAPIError):
                backend.step(_make_request())

    def test_connection_error_raises_transient(self):
        """requests.RequestException (timeout, connection refused) → TransientAPIError."""
        import requests as req_lib
        backend = self._make_backend()
        with patch("requests.post", side_effect=req_lib.ConnectionError("refused")):
            with pytest.raises(TransientAPIError):
                backend.step(_make_request())

    # ---------- _try_parse_single_tool_call ----------------------------------

    def test_parse_function_call_form(self):
        """tool_name({"key": value}) form is parsed."""
        from ai.llm.adapters.ollama_backend import _try_parse_single_tool_call
        result = _try_parse_single_tool_call('ln_pay({"bolt11": "lnbc100"})')
        assert result == ("ln_pay", {"bolt11": "lnbc100"})

    def test_parse_kwargs_form(self):
        """tool_name(key=val, key=val) form is parsed."""
        from ai.llm.adapters.ollama_backend import _try_parse_single_tool_call
        result = _try_parse_single_tool_call("ln_getinfo(node=1)")
        assert result == ("ln_getinfo", {"node": 1})

    def test_parse_space_form(self):
        """tool_name key=val key=val (space-separated) form is parsed."""
        from ai.llm.adapters.ollama_backend import _try_parse_single_tool_call
        result = _try_parse_single_tool_call("ln_getinfo node=1")
        assert result == ("ln_getinfo", {"node": 1})

    def test_parse_returns_none_for_plain_text(self):
        """Plain text with no tool-call pattern returns None."""
        from ai.llm.adapters.ollama_backend import _try_parse_single_tool_call
        assert _try_parse_single_tool_call("The node is online.") is None

    def test_parse_empty_string(self):
        from ai.llm.adapters.ollama_backend import _try_parse_single_tool_call
        assert _try_parse_single_tool_call("") is None
