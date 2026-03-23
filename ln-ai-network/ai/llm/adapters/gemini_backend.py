"""Gemini backend adapter for the Lightning Agent LLM interface.

Implements LLMBackend.step() for Google Gemini, converting between the
OpenAI-style message/tool format used throughout the codebase and the
Gemini API's native format, then normalizing the response back.

Format conversion overview:
  OpenAI messages          → Gemini Contents
  OpenAI tool schemas      → Gemini FunctionDeclarations
  Gemini function_call     → LLMResponse(type="tool_call", tool_calls=[...])
  Gemini text parts        → LLMResponse(type="final", content="...")

System messages:
  Gemini uses system_instruction at the config level, not in the contents
  list. All OpenAI system messages are extracted and concatenated into a
  single system_instruction string.

Tool result messages:
  OpenAI sends tool results as role="tool" messages with content=JSON.
  Gemini sends them as role="user" parts with a function_response object.

Automatic function calling:
  Disabled (AutomaticFunctionCallingConfig(disable=True)) so we retain
  full control over when tools are called and how results are handled.
  The agent/pipeline loop manages the tool execution and history manually.
"""
from __future__ import annotations

import json
import os
from typing import Any, List

from ai.llm.base import (
    AuthError,
    LLMBackend,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    PermanentAPIError,
    RateLimitError,
    ToolCall,
    TransientAPIError,
)


# ---------------------------------------------------------------------------
# Schema conversion: OpenAI JSON Schema → Gemini Schema objects
# ---------------------------------------------------------------------------

# Maps OpenAI JSON Schema type names to Gemini's uppercase equivalents.
# "string" is the fallback for any unrecognized type.
_OPENAI_TO_GEMINI_TYPE = {
    "string": "STRING",
    "number": "NUMBER",
    "integer": "INTEGER",
    "boolean": "BOOLEAN",
    "array": "ARRAY",
    "object": "OBJECT",
}


def _convert_schema(schema: dict) -> Any:
    """
    Recursively convert an OpenAI JSON Schema dict to a Gemini Schema object.

    Handles nested object properties and array items. Preserves description,
    enum, and required fields. Falls back to STRING for unknown types.
    """
    from google.genai import types  # Deferred import: only loaded when Gemini is selected

    t = _OPENAI_TO_GEMINI_TYPE.get(schema.get("type", "string"), "STRING")
    kwargs: dict[str, Any] = {"type": t}

    if "description" in schema:
        kwargs["description"] = schema["description"]

    if t == "OBJECT":
        # Recursively convert each property's schema
        props = schema.get("properties", {})
        kwargs["properties"] = {k: _convert_schema(v) for k, v in props.items()}
        if "required" in schema:
            kwargs["required"] = schema["required"]

    if t == "ARRAY":
        # Convert the items schema for typed array elements
        items = schema.get("items", {})
        kwargs["items"] = _convert_schema(items)

    if "enum" in schema:
        kwargs["enum"] = schema["enum"]

    return types.Schema(**kwargs)


def _openai_tools_to_gemini(tools: List[dict]) -> Any:
    """
    Convert a list of OpenAI function-calling tool dicts to a Gemini Tool object.

    Handles both formats:
      {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}
      {"name": ..., "description": ..., "parameters": ...}  (bare, no "function" wrapper)
    """
    from google.genai import types

    declarations = []
    for t in tools:
        # Normalize: prefer the nested "function" key, fall back to the dict itself
        fn = t.get("function", t)
        params_schema = fn.get("parameters", {})
        declarations.append(
            types.FunctionDeclaration(
                name=fn["name"],
                description=fn.get("description", ""),
                # Only convert if a non-empty parameters schema is present
                parameters=_convert_schema(params_schema) if params_schema else None,
            )
        )
    return types.Tool(function_declarations=declarations)


# ---------------------------------------------------------------------------
# Message conversion: OpenAI messages → Gemini Contents
# ---------------------------------------------------------------------------

def _openai_messages_to_gemini(messages: List[dict]) -> tuple[str, list]:
    """
    Split an OpenAI messages list into (system_instruction, gemini_contents).

    OpenAI role mapping → Gemini:
      system    → extracted as system_instruction (not in contents)
      user      → role="user", text part
      assistant → role="model", text + optional function_call parts
      tool      → role="user", function_response part

    Note: Gemini only supports "user" and "model" roles in the contents list.
    Tool results must be wrapped in a user-role function_response part.
    """
    from google.genai import types

    system_parts: list[str] = []
    contents: list = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content") or ""

        if role == "system":
            # System messages are extracted and joined as system_instruction
            if content:
                system_parts.append(content)
            continue

        if role == "user":
            contents.append(types.Content(role="user", parts=[types.Part(text=content)]))

        elif role == "assistant":
            # Assistant turns may contain both text and tool call requests
            parts = []
            if content:
                parts.append(types.Part(text=content))
            # Convert any embedded tool_calls (from prior OpenAI-format history)
            for tc in msg.get("tool_calls", []):
                fn = tc.get("function", {})
                args = fn.get("arguments", "{}")
                try:
                    args_dict = json.loads(args) if isinstance(args, str) else args
                except Exception:
                    args_dict = {}
                parts.append(types.Part(
                    function_call=types.FunctionCall(name=fn["name"], args=args_dict)
                ))
            if parts:
                contents.append(types.Content(role="model", parts=parts))

        elif role == "tool":
            # Tool results: sent back as user-role function_response parts
            name = msg.get("name", "tool")
            raw = content
            try:
                response_data = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                response_data = {"result": raw}
            contents.append(types.Content(
                role="user",
                parts=[types.Part(function_response=types.FunctionResponse(
                    name=name,
                    # Gemini requires the response to be a dict
                    response=response_data if isinstance(response_data, dict) else {"result": response_data},
                ))],
            ))

    system_instruction = "\n\n".join(system_parts) if system_parts else ""
    return system_instruction, contents


# ---------------------------------------------------------------------------
# GeminiBackend
# ---------------------------------------------------------------------------

class GeminiBackend(LLMBackend):
    """
    LLMBackend implementation for the Google Gemini API (google-genai SDK).

    Env vars:
      GEMINI_API_KEY     — required; your Google AI Studio API key
      GEMINI_MODEL       — model name (default: "gemini-2.5-flash")
      GEMINI_TIMEOUT_S   — HTTP timeout in seconds (default: 180)

    The google-genai package is imported lazily in __init__ so a missing
    installation only raises when this backend is actually selected.
    """

    def __init__(self, model: str | None = None) -> None:
        try:
            from google import genai
            from google.genai import types
        except ImportError as e:
            raise RuntimeError(
                "The 'google-genai' Python package is not installed. "
                "Run: pip install google-genai"
            ) from e

        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY environment variable is not set.")

        timeout_s = float(os.getenv("GEMINI_TIMEOUT_S", "180"))

        # Store references for use in step() without re-importing
        self._genai = genai
        self._types = types
        self.client = genai.Client(
            api_key=api_key,
            # Gemini SDK takes timeout in milliseconds
            http_options=types.HttpOptions(timeout=int(timeout_s * 1000)),
        )
        self.model = model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    def step(self, request: LLMRequest) -> LLMResponse:
        """
        Send a request to Gemini and return a normalized LLMResponse.

        Converts the OpenAI-format request to Gemini format, sends it, then
        parses the response back into our normalized LLMResponse shape.
        """
        types = self._types

        system_instruction, contents = _openai_messages_to_gemini(request.messages)

        config_kwargs: dict[str, Any] = {
            "temperature": request.temperature,
            "max_output_tokens": request.max_output_tokens,
            # Disable Gemini's automatic function execution — we handle tool calls manually
            "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
        }

        if system_instruction:
            config_kwargs["system_instruction"] = system_instruction

        if request.tools:
            config_kwargs["tools"] = [_openai_tools_to_gemini(request.tools)]

        config = types.GenerateContentConfig(**config_kwargs)

        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=contents,
                config=config,
            )
        except Exception as e:
            # Normalize provider exceptions to our error taxonomy
            err_str = str(e).lower()
            if "401" in err_str or "api key" in err_str or "invalid key" in err_str:
                raise AuthError(f"Gemini auth error: {e}") from e
            if "429" in err_str or "quota" in err_str or "rate" in err_str:
                raise RateLimitError(f"Gemini rate limit: {e}") from e
            if "500" in err_str or "503" in err_str or "timeout" in err_str:
                raise TransientAPIError(f"Gemini server error: {e}") from e
            raise PermanentAPIError(f"Gemini API error: {e}") from e

        return self._parse_response(response)

    def _parse_response(self, response: Any) -> LLMResponse:
        """
        Convert a Gemini GenerateContentResponse to a normalized LLMResponse.

        Scans all parts in the first candidate:
          - function_call parts → ToolCall entries (type="tool_call")
          - text parts          → joined into content string (type="final")
        If both exist in one response, tool_calls take precedence.
        """
        usage = LLMUsage(
            prompt_tokens=getattr(response.usage_metadata, "prompt_token_count", 0) or 0,
            output_tokens=getattr(response.usage_metadata, "candidates_token_count", 0) or 0,
            total_tokens=getattr(response.usage_metadata, "total_token_count", 0) or 0,
        )

        if not response.candidates:
            # Empty response: return empty final (should not normally happen)
            return LLMResponse(
                type="final",
                tool_calls=[],
                content="",
                reasoning=None,
                usage=usage,
            )

        candidate = response.candidates[0]
        parts = candidate.content.parts if candidate.content else []

        tool_calls: list[ToolCall] = []
        text_parts: list[str] = []

        for part in parts:
            if hasattr(part, "function_call") and part.function_call:
                fc = part.function_call
                args = dict(fc.args) if fc.args else {}
                tool_calls.append(ToolCall(name=fc.name, args=args))
            elif hasattr(part, "text") and part.text:
                text_parts.append(part.text)

        if tool_calls:
            return LLMResponse(
                type="tool_call",   # Fixed: was "tool_use" (wrong enum value)
                tool_calls=tool_calls,
                content=None,
                reasoning=None,
                usage=usage,
            )

        return LLMResponse(
            type="final",
            tool_calls=[],
            content="".join(text_parts),
            reasoning=None,
            usage=usage,
        )
