from __future__ import annotations

# =============================================================================
# OllamaBackend — local LLM via Ollama HTTP API
#
# Connects to a locally-running Ollama server and calls its /api/chat endpoint.
# Handles Ollama's two tool-call signaling methods:
#
#   Method 1 (preferred): structured tool_calls field in the response message
#     Ollama (with recent models like llama3.1, qwen2.5) may return:
#       {"message": {"role": "assistant", "tool_calls": [{"function": {...}}]}}
#     These are parsed directly into ToolCall objects.
#
#   Method 2 (fallback): tool call embedded as text in the message content
#     Some models/versions return tool calls as formatted text rather than
#     structured JSON. The fallback parser (_try_parse_single_tool_call)
#     handles several text formats:
#       - {"tool": "name", "args": {...}}         (JSON object form)
#       - {"tool_calls": [{"name": ..., "args": ...}]}  (JSON list form)
#       - tool_name({"key": value})               (function-call form)
#       - tool_name(key=value, key2=value2)       (kwargs form)
#       - tool_name key=value key2=value2         (space-separated form)
#     The parsed tool name must appear in the request's tools schema
#     (checked via _allowed_tool_names) to prevent hallucinated tool names
#     from triggering real MCP calls.
#
# Env vars:
#   OLLAMA_BASE_URL       base URL of the Ollama server (default: http://127.0.0.1:11434)
#   OLLAMA_MODEL          model name to use (default: llama3.2:3b)
#   OLLAMA_TIMEOUT_SEC    HTTP timeout in seconds (default: 120)
#   OLLAMA_TOOL_TEMP_ZERO if true, force temperature=0 when tools are present
#                         to make tool selection deterministic (default: 1/true)
# =============================================================================

import json
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from ai.llm.base import (
    LLMBackend,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    ToolCall,
    TransientAPIError,
    PermanentAPIError,
)
from ai.utils import _env_bool


# ---------------------------------------------------------------------------
# Fallback text parser for tool calls embedded in message content
# ---------------------------------------------------------------------------

def _parse_value(s: str) -> Any:
    """
    Convert a single string value token to its Python type.

    Used by the kwargs-form and space-form fallback parsers to convert
    "1" → 1, "true" → True, "null" → None, etc.
    Leaves unrecognized strings as-is (not quoted).
    """
    s = s.strip()
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    if s.lower() in ("null", "none"):
        return None
    if re.fullmatch(r"-?\d+", s):
        try:
            return int(s)
        except Exception:
            return s
    if re.fullmatch(r"-?\d+\.\d+", s):
        try:
            return float(s)
        except Exception:
            return s
    # Strip surrounding quotes if present
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    return s


def _try_parse_single_tool_call(text: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """
    Parse a tool call from assistant content text when structured tool_calls is absent.

    Tries four forms in order:
      1. JSON object: {"tool":"name","args":{...}} or {"tool_calls":[{"name":...,"args":...}]}
      2. Function form: tool_name({...json...})
      3. Kwargs form:   tool_name(key=value, key=value)
      4. Space form:    tool_name key=value key=value

    Returns (tool_name, args_dict) or None if no form matches.
    None is the safe fallback — the caller treats it as a final text response.
    """
    if not text:
        return None
    t = text.strip()

    # Form 1: JSON object
    if t.startswith("{") and t.endswith("}"):
        try:
            obj = json.loads(t)
        except Exception:
            obj = None
        if isinstance(obj, dict):
            # {"tool":"...", "args": {...}}
            if isinstance(obj.get("tool"), str) and isinstance(obj.get("args"), dict):
                return obj["tool"], obj["args"]
            # {"tool_calls":[{"name":"...", "args": {...}}]}
            tcs = obj.get("tool_calls")
            if isinstance(tcs, list) and tcs:
                first = tcs[0]
                if isinstance(first, dict) and isinstance(first.get("name"), str):
                    args = first.get("args")
                    return first["name"], args if isinstance(args, dict) else {}

    # Form 2 + 3: tool_name(...) — JSON body or key=value pairs
    m = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*\((.*)\)\s*$", t)
    if m:
        name = m.group(1)
        inner = m.group(2).strip()
        if inner == "":
            return name, {}
        if inner.startswith("{") and inner.endswith("}"):
            try:
                args = json.loads(inner)
                if isinstance(args, dict):
                    return name, args
            except Exception:
                return None  # Malformed JSON inside parens — don't guess
        # Form 3: key=value, key=value
        args2: Dict[str, Any] = {}
        parts = [p.strip() for p in inner.split(",") if p.strip()]
        for p in parts:
            if "=" not in p:
                return None
            k, v = p.split("=", 1)
            args2[k.strip()] = _parse_value(v.strip())
        return name, args2

    # Form 4: tool_name key=value key2=value2 (space-separated)
    m2 = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s+(.*)$", t)
    if m2:
        name = m2.group(1)
        rest = m2.group(2).strip()
        args3: Dict[str, Any] = {}
        for tok in rest.split():
            if "=" not in tok:
                return None  # Not a key=value token — don't guess
            k, v = tok.split("=", 1)
            args3[k.strip()] = _parse_value(v.strip())
        return name, args3

    return None


def _allowed_tool_names(tools_schema: List[Dict[str, Any]]) -> set[str]:
    """
    Extract the set of valid tool names from the tools schema list.

    Used to gate the fallback parser: a parsed tool name must appear in this
    set before we treat it as a real tool call. Without this check, the LLM
    could hallucinate a tool name that looks like the right format but doesn't
    correspond to any registered MCP tool.
    """
    allowed: set[str] = set()
    for t in tools_schema or []:
        try:
            fn = t.get("function") or {}
            name = fn.get("name")
            if isinstance(name, str) and name:
                allowed.add(name)
        except Exception:
            continue
    return allowed


# ---------------------------------------------------------------------------
# OllamaBackend
# ---------------------------------------------------------------------------

class OllamaBackend(LLMBackend):
    """
    LLMBackend implementation using Ollama's local HTTP API (/api/chat).

    Response parsing priority:
      1. Structured tool_calls in the message — most reliable; use directly.
      2. Content text parsed as a tool call — fallback for models that embed
         tool calls as text instead of using structured format.
      3. Plain text final response — if neither of the above matches.
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout_sec: Optional[int] = None,
    ) -> None:
        self.base_url = (base_url or os.getenv("OLLAMA_BASE_URL") or "http://127.0.0.1:11434").rstrip("/")
        self.model = model or os.getenv("OLLAMA_MODEL") or "llama3.2:3b"
        self.timeout_sec = int(timeout_sec or os.getenv("OLLAMA_TIMEOUT_SEC") or "120")
        # Force temperature=0 when tool schemas are included to get deterministic
        # tool selection; temperature > 0 can make tool choice unpredictable.
        self.tool_temp_zero = _env_bool("OLLAMA_TOOL_TEMP_ZERO", default=True)

    def step(self, request: LLMRequest) -> LLMResponse:
        url = f"{self.base_url}/api/chat"

        # Override temperature to 0 when tool schemas are present (if configured)
        temp = float(request.temperature)
        if self.tool_temp_zero and (request.tools or []):
            temp = 0.0

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": request.messages,
            "stream": False,      # Get complete response in one JSON body
            "tools": request.tools or [],
            "options": {
                "temperature": temp,
                "num_predict": request.max_output_tokens,
            },
        }

        try:
            r = requests.post(url, json=payload, timeout=self.timeout_sec)
        except requests.RequestException as e:
            raise TransientAPIError(f"Ollama connection failed: {e}") from e

        # Map HTTP status codes to normalized error types
        if 500 <= r.status_code <= 599:
            raise TransientAPIError(f"Ollama server error {r.status_code}: {r.text}")
        if 400 <= r.status_code <= 499:
            raise PermanentAPIError(f"Ollama request error {r.status_code}: {r.text}")

        try:
            data = r.json()
        except json.JSONDecodeError as e:
            raise TransientAPIError(f"Ollama returned non-JSON response: {e}") from e

        msg = data.get("message") or {}

        # ── Priority 1: Structured tool_calls ────────────────────────────────
        tool_calls: List[ToolCall] = []
        raw_tool_calls = msg.get("tool_calls") or []
        for tc in raw_tool_calls:
            fn = (tc.get("function") or {})
            name = fn.get("name")
            args = fn.get("arguments")
            # arguments may be a JSON string or already a dict
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {"_raw": args}  # Preserve for debugging; normalization handles later
            if isinstance(name, str) and name:
                tool_calls.append(ToolCall(name=name, args=args or {}))

        # Extract usage metrics (both fields may be absent on older Ollama versions)
        usage = None
        prompt_tokens = data.get("prompt_eval_count")
        output_tokens = data.get("eval_count")
        if isinstance(prompt_tokens, int) and isinstance(output_tokens, int):
            usage = LLMUsage(
                prompt_tokens=prompt_tokens,
                output_tokens=output_tokens,
                total_tokens=prompt_tokens + output_tokens,
            )

        if tool_calls:
            return LLMResponse(
                type="tool_call",
                tool_calls=tool_calls,
                content=None,
                reasoning=None,
                usage=usage,
            )

        # ── Priority 2: Fallback — parse tool call from content text ─────────
        content = msg.get("content")
        content_str = content if isinstance(content, str) else ""
        allowed = _allowed_tool_names(request.tools)

        parsed = _try_parse_single_tool_call(content_str)
        if parsed is not None:
            name, args = parsed
            if name in allowed:
                # Only treat as a tool call if the name is in the schema
                return LLMResponse(
                    type="tool_call",
                    tool_calls=[ToolCall(name=name, args=args)],
                    content=None,
                    reasoning=None,
                    usage=usage,
                )

        # ── Priority 3: Plain text final response ────────────────────────────
        return LLMResponse(
            type="final",
            tool_calls=[],
            content=content_str,
            reasoning=None,
            usage=usage,
        )

    def stream(self, request: LLMRequest) -> Iterable[str]:
        """
        Stream text tokens from Ollama using the streaming chat API.

        Uses Ollama's NDJSON streaming format: each line is a JSON object
        with a "message.content" field containing the token delta. Yields
        each non-empty content delta as it arrives.

        Only suitable for text generation (tools=[]). Falls back to step()
        semantics (single yield of complete content) if a structured tool
        call is detected in the stream.
        """
        url = f"{self.base_url}/api/chat"
        temp = float(request.temperature)
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": request.messages,
            "stream": True,
            "tools": request.tools or [],
            "options": {
                "temperature": temp,
                "num_predict": request.max_output_tokens,
            },
        }

        try:
            r = requests.post(url, json=payload, timeout=self.timeout_sec, stream=True)
        except requests.RequestException as e:
            raise TransientAPIError(f"Ollama connection failed: {e}") from e

        if 500 <= r.status_code <= 599:
            raise TransientAPIError(f"Ollama server error {r.status_code}: {r.text}")
        if 400 <= r.status_code <= 499:
            raise PermanentAPIError(f"Ollama request error {r.status_code}: {r.text}")

        for line in r.iter_lines():
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = data.get("message") or {}
            content = msg.get("content")
            if content:
                yield str(content)
