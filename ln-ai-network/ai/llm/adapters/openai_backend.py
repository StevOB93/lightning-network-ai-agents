import json
import os
from typing import Any, Dict, List, Optional

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

    def __init__(self, model: Optional[str] = None) -> None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise AuthError("OPENAI_API_KEY not set")

        self.client = OpenAI(api_key=api_key)
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o")

    def step(self, request: LLMRequest) -> LLMResponse:
        try:
            kwargs: Dict[str, Any] = {
                "model": self.model,
                "messages": request.messages,
                "temperature": request.temperature,
                "max_tokens": request.max_output_tokens,
            }
            if request.tools:
                kwargs["tools"] = request.tools

            response = self.client.chat.completions.create(**kwargs)
        except Exception as e:
            msg = str(e)
            if "401" in msg or "authentication" in msg.lower() or "api_key" in msg.lower():
                raise AuthError(msg) from e
            if "429" in msg or "rate_limit" in msg.lower():
                raise RateLimitError(msg) from e
            if any(c in msg for c in ("500", "502", "503", "504", "timeout", "connect")):
                raise TransientAPIError(msg) from e
            raise PermanentAPIError(msg) from e

        choice = response.choices[0]

        usage: Optional[LLMUsage] = None
        if response.usage:
            usage = LLMUsage(
                prompt_tokens=response.usage.prompt_tokens,
                output_tokens=response.usage.completion_tokens,
                total_tokens=response.usage.total_tokens,
            )

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
                content=choice.message.content,
                reasoning=None,
                usage=usage,
            )

        return LLMResponse(
            type="final",
            tool_calls=[],
            content=choice.message.content,
            reasoning=None,
            usage=usage,
        )