from __future__ import annotations

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


def _safe_json_loads(maybe_json: Any) -> Dict[str, Any]:
    if isinstance(maybe_json, dict):
        return maybe_json
    if isinstance(maybe_json, str):
        try:
            parsed = json.loads(maybe_json)
            return parsed if isinstance(parsed, dict) else {"_raw": parsed}
        except Exception:
            return {"_raw": maybe_json}
    return {"_raw": maybe_json}


def _extract_retry_after_seconds(exc: Exception) -> Optional[float]:
    # Best-effort: different openai versions expose headers differently.
    for attr in ("headers", "response", "http_response"):
        obj = getattr(exc, attr, None)
        if obj is None:
            continue
        headers = getattr(obj, "headers", None) or getattr(obj, "headers", None)
        if not headers:
            continue
        ra = headers.get("retry-after") or headers.get("Retry-After")
        if ra:
            try:
                return float(ra)
            except Exception:
                return None
    return None


class OpenAIBackend(LLMBackend):
    """
    OpenAI adapter. All OpenAI-specific behavior stays here.
    Agent core only sees normalized responses + normalized errors.
    """

    def __init__(self) -> None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set")

        self._model = os.getenv("OPENAI_MODEL", "gpt-4o")
        self._timeout_s = float(os.getenv("OPENAI_TIMEOUT_S", "60"))
        self.client = OpenAI(api_key=api_key)

    def step(self, request: LLMRequest) -> LLMResponse:
        try:
            # NOTE: OpenAI SDK and parameter names can evolve;
            # keep this adapter as the only place to update.
            resp = self.client.chat.completions.create(
                model=self._model,
                messages=request.messages,
                tools=request.tools,
                temperature=request.temperature,
                max_tokens=request.max_output_tokens,
                timeout=self._timeout_s,
            )

            choice = resp.choices[0]
            msg = choice.message

            usage = None
            if getattr(resp, "usage", None):
                usage = LLMUsage(
                    prompt_tokens=int(getattr(resp.usage, "prompt_tokens", 0) or 0),
                    output_tokens=int(getattr(resp.usage, "completion_tokens", 0) or 0),
                    total_tokens=int(getattr(resp.usage, "total_tokens", 0) or 0),
                )

            # Tool calls (can be multiple)
            tool_calls: List[ToolCall] = []
            if choice.finish_reason == "tool_calls" and getattr(msg, "tool_calls", None):
                for tc in msg.tool_calls:
                    name = tc.function.name
                    args = _safe_json_loads(tc.function.arguments)
                    tool_calls.append(ToolCall(name=name, args=args))

                return LLMResponse(
                    type="tool_call",
                    tool_calls=tool_calls,
                    content=None,
                    reasoning=msg.content,  # may be None
                    usage=usage,
                )

            # Final
            return LLMResponse(
                type="final",
                tool_calls=[],
                content=msg.content,
                reasoning=None,
                usage=usage,
            )

        except Exception as e:
            # Map provider errors -> normalized errors.
            # This is intentionally best-effort and contained to the adapter.
            name = e.__class__.__name__.lower()
            status = getattr(e, "status_code", None)

            # Auth
            if "authentication" in name or "auth" in name or status in (401, 403):
                raise AuthError(str(e)) from e

            # Rate limit
            if "ratelimit" in name or "rate_limit" in name or status == 429:
                raise RateLimitError(str(e), retry_after_s=_extract_retry_after_seconds(e)) from e

            # Retryable
            if "timeout" in name or "apitimeout" in name or "connection" in name or (isinstance(status, int) and status >= 500):
                raise TransientAPIError(str(e)) from e

            # Likely non-retryable request/schema issues
            if isinstance(status, int) and 400 <= status < 500:
                raise PermanentAPIError(str(e)) from e

            # Unknown: treat as transient to avoid hot-failing forever
            raise TransientAPIError(str(e)) from e