"""Gemini backend adapter for the Lightning Agent LLM interface.

Implements the standard LLMBackend.step() interface so Gemini can be used
by any pipeline stage (Translator, Planner) or the legacy agent.
Converts OpenAI-style messages and tool schemas to Gemini format and back.
"""
from __future__ import annotations

import json
import os
from typing import Any, List

from ai.llm.base import (
    LLMBackend,
    LLMError,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    ToolCall,
    TransientAPIError,
    PermanentAPIError,
    AuthError,
)


# ---------------------------------------------------------------------------
# Schema conversion: OpenAI → Gemini
# ---------------------------------------------------------------------------

_OPENAI_TO_GEMINI_TYPE = {
    "string": "STRING",
    "number": "NUMBER",
    "integer": "INTEGER",
    "boolean": "BOOLEAN",
    "array": "ARRAY",
    "object": "OBJECT",
}


def _convert_schema(schema: dict) -> Any:
    """Recursively convert an OpenAI JSON Schema dict to a Gemini Schema object."""
    from google.genai import types  # lazy import

    t = _OPENAI_TO_GEMINI_TYPE.get(schema.get("type", "string"), "STRING")
    kwargs: dict[str, Any] = {"type": t}

    if "description" in schema:
        kwargs["description"] = schema["description"]

    if t == "OBJECT":
        props = schema.get("properties", {})
        kwargs["properties"] = {k: _convert_schema(v) for k, v in props.items()}
        if "required" in schema:
            kwargs["required"] = schema["required"]

    if t == "ARRAY":
        items = schema.get("items", {})
        kwargs["items"] = _convert_schema(items)

    if "enum" in schema:
        kwargs["enum"] = schema["enum"]

    return types.Schema(**kwargs)


def _openai_tools_to_gemini(tools: List[dict]) -> Any:
    """Convert a list of OpenAI function-calling tool dicts to a Gemini Tool."""
    from google.genai import types  # lazy import

    declarations = []
    for t in tools:
        fn = t.get("function", t)  # handle both {"type":"function","function":{...}} and bare
        params_schema = fn.get("parameters", {})
        declarations.append(
            types.FunctionDeclaration(
                name=fn["name"],
                description=fn.get("description", ""),
                parameters=_convert_schema(params_schema) if params_schema else None,
            )
        )
    return types.Tool(function_declarations=declarations)


# ---------------------------------------------------------------------------
# Message conversion: OpenAI → Gemini
# ---------------------------------------------------------------------------

def _openai_messages_to_gemini(messages: List[dict]) -> tuple[str, list]:
    """
    Split OpenAI messages into (system_instruction, gemini_contents).

    OpenAI roles:   system | user | assistant | tool
    Gemini roles:   user   | model
    """
    from google.genai import types  # lazy import

    system_parts: list[str] = []
    contents: list = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content") or ""

        if role == "system":
            if content:
                system_parts.append(content)
            continue

        if role == "user":
            contents.append(types.Content(role="user", parts=[types.Part(text=content)]))

        elif role == "assistant":
            parts = []
            if content:
                parts.append(types.Part(text=content))
            for tc in msg.get("tool_calls", []):
                fn = tc.get("function", {})
                args = fn.get("arguments", "{}")
                try:
                    args_dict = json.loads(args) if isinstance(args, str) else args
                except Exception:
                    args_dict = {}
                parts.append(types.Part(function_call=types.FunctionCall(name=fn["name"], args=args_dict)))
            if parts:
                contents.append(types.Content(role="model", parts=parts))

        elif role == "tool":
            # Tool results go back as user messages with function_response
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
                    response=response_data if isinstance(response_data, dict) else {"result": response_data},
                ))],
            ))

    system_instruction = "\n\n".join(system_parts) if system_parts else ""
    return system_instruction, contents


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------

class GeminiBackend(LLMBackend):
    """LLMBackend implementation that calls the Google Gemini API."""

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

        self._genai = genai
        self._types = types
        self.client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=int(timeout_s * 1000)),
        )
        self.model = model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    # ------------------------------------------------------------------
    # Standard pipeline interface
    # ------------------------------------------------------------------

    def step(self, request: LLMRequest) -> LLMResponse:
        """Execute one LLM step: send messages + tools, return a response."""
        types = self._types

        system_instruction, contents = _openai_messages_to_gemini(request.messages)

        config_kwargs: dict[str, Any] = {
            "temperature": request.temperature,
            "max_output_tokens": request.max_output_tokens,
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
            err_str = str(e).lower()
            if "401" in err_str or "api key" in err_str or "invalid key" in err_str:
                raise AuthError(f"Gemini auth error: {e}") from e
            if "429" in err_str or "quota" in err_str or "rate" in err_str:
                raise TransientAPIError(f"Gemini rate limit: {e}") from e
            if "500" in err_str or "503" in err_str or "timeout" in err_str:
                raise TransientAPIError(f"Gemini server error: {e}") from e
            raise PermanentAPIError(f"Gemini API error: {e}") from e

        return self._parse_response(response)

    def _parse_response(self, response: Any) -> LLMResponse:
        """Convert a Gemini GenerateContentResponse to our normalized LLMResponse."""
        usage = LLMUsage(
            prompt_tokens=getattr(response.usage_metadata, "prompt_token_count", 0) or 0,
            output_tokens=getattr(response.usage_metadata, "candidates_token_count", 0) or 0,
            total_tokens=getattr(response.usage_metadata, "total_token_count", 0) or 0,
        )

        if not response.candidates:
            return LLMResponse(type="final", content="", usage=usage)

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
            return LLMResponse(type="tool_use", tool_calls=tool_calls, usage=usage)

        return LLMResponse(type="final", content="".join(text_parts), usage=usage)
