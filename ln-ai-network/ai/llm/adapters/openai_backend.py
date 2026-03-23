from __future__ import annotations

# =============================================================================
# OpenAIBackend — GPT-4o and compatible models via the OpenAI Python SDK
#
# Uses the openai SDK's chat.completions.create() method. The OpenAI API
# natively uses the same message/tool format we use internally (our format
# is modeled on OpenAI's), so no format conversion is needed.
#
# Tool calls:
#   When finish_reason="tool_calls", choice.message.tool_calls contains one or
#   more tool call objects. Each has function.name and function.arguments (a
#   JSON string). _parse_args() decodes the arguments string.
#
# Error mapping:
#   The openai SDK raises a hierarchy of exceptions. We map them to our
#   normalized LLMError subclasses by inspecting the exception message string,
#   since the SDK's exception hierarchy varies across SDK versions.
#
# Compatibility:
#   Also works with Azure OpenAI and any endpoint that implements the OpenAI
#   chat completions API (e.g. LM Studio, vLLM with OpenAI-compatible mode)
#   by setting OPENAI_API_KEY and pointing the SDK to the right base URL.
#
# Env vars:
#   OPENAI_API_KEY   — required
#   OPENAI_MODEL     — model name (default: "gpt-4o")
# =============================================================================

import json
import os
from typing import Any, Dict, Iterable, List, Optional

from openai import OpenAI
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


def _parse_args(raw: Any) -> Dict[str, Any]:
    """
    Decode tool call arguments from the OpenAI response.

    The OpenAI API returns function.arguments as a JSON string (not a dict).
    This function handles all three shapes that may appear:
      None   → {}
      dict   → returned as-is (defensive: some SDKs may pre-parse)
      str    → parsed as JSON; non-dict result → {}
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            obj = json.loads(raw)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
    return {}


class OpenAIBackend(LLMBackend):
    """
    LLMBackend implementation for OpenAI's chat completions API.

    No message format conversion needed — our internal format mirrors OpenAI's.
    Tool schemas are passed directly as the `tools` parameter.
    """

    def __init__(self, model: Optional[str] = None) -> None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise AuthError("OPENAI_API_KEY not set")

        self.client = OpenAI(api_key=api_key)
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o")

    def step(self, request: LLMRequest) -> LLMResponse:
        """
        Send a chat completions request to OpenAI and return a normalized response.

        Tools are only included in the API call if the request has tools — this
        avoids unnecessary schema overhead for pure text completion calls (e.g.
        the Translator and Summarizer stages which use tools=[]).
        """
        try:
            kwargs: Dict[str, Any] = {
                "model": self.model,
                "messages": request.messages,
                "temperature": request.temperature,
                "max_tokens": request.max_output_tokens,
            }
            # Only pass `tools` when there are tools — avoids API errors on
            # some model endpoints that don't support the tools parameter.
            if request.tools:
                kwargs["tools"] = request.tools

            response = self.client.chat.completions.create(**kwargs)

        except Exception as e:
            # Map openai SDK exceptions to normalized error types by string matching.
            # This is fragile but necessary because the SDK's exception hierarchy
            # changed between versions. Ordered from most specific to least specific.
            msg = str(e)
            if "401" in msg or "authentication" in msg.lower() or "api_key" in msg.lower():
                raise AuthError(msg) from e
            if "429" in msg or "rate_limit" in msg.lower():
                raise RateLimitError(msg) from e
            if any(c in msg for c in ("500", "502", "503", "504", "timeout", "connect")):
                raise TransientAPIError(msg) from e
            raise PermanentAPIError(msg) from e

        choice = response.choices[0]

        # Extract token usage if available (strongly recommended for rate limiting)
        usage: Optional[LLMUsage] = None
        if response.usage:
            usage = LLMUsage(
                prompt_tokens=response.usage.prompt_tokens,
                output_tokens=response.usage.completion_tokens,
                total_tokens=response.usage.total_tokens,
            )

        # OpenAI signals a tool call via finish_reason="tool_calls"
        if choice.finish_reason == "tool_calls" and choice.message.tool_calls:
            tool_calls = [
                ToolCall(
                    name=tc.function.name,
                    args=_parse_args(tc.function.arguments),
                )
                for tc in choice.message.tool_calls
            ]
            return LLMResponse(
                type="tool_call",
                tool_calls=tool_calls,
                content=choice.message.content,  # May contain reasoning text alongside tool calls
                reasoning=None,
                usage=usage,
            )

        # Plain text response (finish_reason="stop" or "length")
        return LLMResponse(
            type="final",
            tool_calls=[],
            content=choice.message.content,
            reasoning=None,
            usage=usage,
        )

    def stream(self, request: LLMRequest) -> Iterable[str]:
        """
        Stream text tokens from OpenAI using the streaming completions API.

        Yields content delta strings as they arrive from the API. Usage stats
        are not available mid-stream; callers that need token counts should
        call step() instead.
        """
        try:
            kwargs: Dict[str, Any] = {
                "model": self.model,
                "messages": request.messages,
                "temperature": request.temperature,
                "max_tokens": request.max_output_tokens,
                "stream": True,
            }
            if request.tools:
                kwargs["tools"] = request.tools

            for chunk in self.client.chat.completions.create(**kwargs):
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    yield delta.content

        except Exception as e:
            msg = str(e)
            if "401" in msg or "authentication" in msg.lower() or "api_key" in msg.lower():
                raise AuthError(msg) from e
            if "429" in msg or "rate_limit" in msg.lower():
                raise RateLimitError(msg) from e
            if any(c in msg for c in ("500", "502", "503", "504", "timeout", "connect")):
                raise TransientAPIError(msg) from e
            raise PermanentAPIError(msg) from e
