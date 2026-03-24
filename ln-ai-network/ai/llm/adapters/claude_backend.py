from __future__ import annotations

# =============================================================================
# ClaudeBackend — Anthropic Claude models via the anthropic Python SDK
#
# Converts our internal OpenAI-style message/tool format to Anthropic's wire
# format, then normalizes the response back to LLMResponse.
#
# Format differences from OpenAI:
#   - System messages are extracted and passed as a top-level `system` param.
#   - Tool results (role="tool") become role="user" messages with a
#     "tool_result" content block (Anthropic does not have a "tool" role).
#   - Assistant messages with tool_calls become role="assistant" messages
#     with "tool_use" content blocks.
#   - Tool schemas use "input_schema" instead of OpenAI's "parameters".
#   - Tool call inputs are returned as dicts (not JSON strings).
#   - stop_reason="tool_use" instead of finish_reason="tool_calls".
#
# Error mapping:
#   anthropic.AuthenticationError  → AuthError
#   anthropic.RateLimitError       → RateLimitError (with retry_after_s if present)
#   anthropic.APIStatusError 5xx   → TransientAPIError
#   anthropic.APIStatusError 4xx   → PermanentAPIError
#   anthropic.APIConnectionError   → TransientAPIError
#   anything else                  → PermanentAPIError
#
# Env vars:
#   ANTHROPIC_API_KEY  — required
#   CLAUDE_MODEL       — model name (default: "claude-opus-4-6")
# =============================================================================

import os
from typing import Any, Dict, Iterable, List, Optional

import anthropic

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

_DEFAULT_MODEL = "claude-opus-4-6"


def _convert_tools(openai_tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Convert OpenAI-style tool schemas to Anthropic format.

    OpenAI:
      {"type": "function", "function": {"name": "...", "description": "...",
                                         "parameters": {...}}}
    Anthropic:
      {"name": "...", "description": "...", "input_schema": {...}}
    """
    result = []
    for t in openai_tools:
        if t.get("type") == "function" and "function" in t:
            fn = t["function"]
            result.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            })
        else:
            # Already in Anthropic format or unknown — pass through as-is
            result.append(t)
    return result


def _convert_messages(
    openai_messages: List[Dict[str, Any]],
) -> tuple[Optional[str], List[Dict[str, Any]]]:
    """
    Convert internal OpenAI-style messages to Anthropic format.

    Returns (system_prompt, anthropic_messages).

    Anthropic differences:
    - No "system" role in the messages array — system prompt is a separate param.
    - No "tool" role — tool results are user messages with tool_result blocks.
    - Assistant tool_calls become tool_use content blocks.
    - Multiple consecutive same-role messages may be merged into one.
    """
    system_parts: List[str] = []
    converted: List[Dict[str, Any]] = []

    for msg in openai_messages:
        role = msg.get("role", "")
        content = msg.get("content")

        if role == "system":
            if content:
                system_parts.append(str(content))
            continue

        if role == "tool":
            # Tool result — becomes a user message with a tool_result block.
            # tool_call_id maps to tool_use_id in Anthropic's format.
            tool_use_id = msg.get("tool_call_id", "unknown")
            tool_content = content if isinstance(content, str) else str(content or "")
            converted.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": tool_content,
                }],
            })
            continue

        if role == "assistant":
            # Check for tool_calls on the message (OpenAI format)
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                blocks: List[Dict[str, Any]] = []
                # Include any text content alongside the tool use blocks
                if content:
                    blocks.append({"type": "text", "text": str(content)})
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    raw_args = fn.get("arguments", {})
                    if isinstance(raw_args, str):
                        import json
                        try:
                            raw_args = json.loads(raw_args)
                        except Exception:
                            raw_args = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": raw_args if isinstance(raw_args, dict) else {},
                    })
                converted.append({"role": "assistant", "content": blocks})
                continue

            # Plain assistant text
            converted.append({
                "role": "assistant",
                "content": str(content) if content is not None else "",
            })
            continue

        if role == "user":
            converted.append({
                "role": "user",
                "content": str(content) if content is not None else "",
            })
            continue

        # Unknown role — skip silently
        continue

    system_prompt = "\n\n".join(system_parts) if system_parts else None
    return system_prompt, converted


def _map_error(exc: Exception) -> LLMError_type:
    """Map Anthropic SDK exceptions to our normalized error taxonomy."""
    if isinstance(exc, anthropic.AuthenticationError):
        raise AuthError(str(exc)) from exc
    if isinstance(exc, anthropic.RateLimitError):
        retry_after: Optional[float] = None
        try:
            retry_after_header = exc.response.headers.get("retry-after")
            if retry_after_header is not None:
                retry_after = float(retry_after_header)
        except Exception:
            pass
        raise RateLimitError(str(exc), retry_after_s=retry_after) from exc
    if isinstance(exc, anthropic.APIStatusError):
        status = getattr(exc, "status_code", 0) or 0
        if status >= 500:
            raise TransientAPIError(str(exc)) from exc
        raise PermanentAPIError(str(exc)) from exc
    if isinstance(exc, anthropic.APIConnectionError):
        raise TransientAPIError(str(exc)) from exc
    raise PermanentAPIError(str(exc)) from exc


# Type alias used only for the _map_error annotation
LLMError_type = None  # not actually used at runtime; _map_error always raises


class ClaudeBackend(LLMBackend):
    """
    LLMBackend implementation for Anthropic Claude models.

    Converts internal OpenAI-style requests to Anthropic's format and
    normalizes the response. Supports both step() and stream().

    Model selection (in priority order):
      1. model parameter passed to __init__() (from create_backend_for_role)
      2. CLAUDE_MODEL env var
      3. Hard-coded default "claude-opus-4-6"
    """

    def __init__(self, model: Optional[str] = None) -> None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key or not api_key.strip():
            raise AuthError(
                "ANTHROPIC_API_KEY is not set. "
                "Set it in your .env file to use LLM_BACKEND=claude."
            )
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model or os.getenv("CLAUDE_MODEL") or _DEFAULT_MODEL

    def step(self, request: LLMRequest) -> LLMResponse:
        """
        Send a messages request to Claude and return a normalized LLMResponse.

        Tool schemas are converted from OpenAI format to Anthropic format.
        Tool call inputs come back as dicts (unlike OpenAI which returns JSON
        strings), so no JSON parsing is needed for the args.
        """
        system_prompt, messages = _convert_messages(request.messages)
        anthropic_tools = _convert_tools(request.tools) if request.tools else []

        try:
            kwargs: Dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "max_tokens": request.max_output_tokens,
                "temperature": request.temperature,
            }
            if system_prompt:
                kwargs["system"] = system_prompt
            if anthropic_tools:
                kwargs["tools"] = anthropic_tools

            response = self.client.messages.create(**kwargs)

        except Exception as exc:
            _map_error(exc)
            # _map_error always raises; this line is unreachable but satisfies mypy
            raise  # pragma: no cover

        # Extract token usage
        usage: Optional[LLMUsage] = None
        if response.usage:
            prompt_tokens = getattr(response.usage, "input_tokens", 0) or 0
            output_tokens = getattr(response.usage, "output_tokens", 0) or 0
            usage = LLMUsage(
                prompt_tokens=prompt_tokens,
                output_tokens=output_tokens,
                total_tokens=prompt_tokens + output_tokens,
            )

        # Anthropic signals a tool call via stop_reason="tool_use"
        if response.stop_reason == "tool_use":
            tool_calls = []
            text_content: Optional[str] = None
            for block in response.content:
                if block.type == "tool_use":
                    tool_calls.append(ToolCall(
                        name=block.name,
                        args=dict(block.input) if isinstance(block.input, dict) else {},
                    ))
                elif block.type == "text" and block.text:
                    text_content = block.text
            return LLMResponse(
                type="tool_call",
                tool_calls=tool_calls,
                content=text_content,
                reasoning=None,
                usage=usage,
            )

        # Plain text response
        text = ""
        for block in response.content:
            if block.type == "text":
                text = block.text
                break
        return LLMResponse(
            type="final",
            tool_calls=[],
            content=text,
            reasoning=None,
            usage=usage,
        )

    def stream(self, request: LLMRequest) -> Iterable[str]:
        """
        Stream text tokens from Claude using the messages stream API.

        Yields content delta strings as they arrive. Only valid for text
        generation (tools=[]); callers should use step() for tool-call requests.
        """
        system_prompt, messages = _convert_messages(request.messages)
        anthropic_tools = _convert_tools(request.tools) if request.tools else []

        try:
            kwargs: Dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "max_tokens": request.max_output_tokens,
                "temperature": request.temperature,
            }
            if system_prompt:
                kwargs["system"] = system_prompt
            if anthropic_tools:
                kwargs["tools"] = anthropic_tools

            with self.client.messages.stream(**kwargs) as stream:
                for text in stream.text_stream:
                    yield text

        except Exception as exc:
            _map_error(exc)
            raise  # pragma: no cover
