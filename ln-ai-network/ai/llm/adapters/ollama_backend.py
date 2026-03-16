from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

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


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _parse_value(s: str) -> Any:
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
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    return s


def _try_parse_single_tool_call(text: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """
    Strictly parse a single tool call from assistant content.
    Supported:
      - {"tool_calls":[{"name":"tool","args":{...}}, ...]}
      - {"tool":"tool","args":{...}}
      - tool_name({...json...})
      - tool_name(key=value, key=value)
      - tool_name key=value key=value
    """
    if not text:
        return None
    t = text.strip()

    # JSON envelope forms
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

    # tool_name(...)
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
                return None
        args2: Dict[str, Any] = {}
        parts = [p.strip() for p in inner.split(",") if p.strip()]
        for p in parts:
            if "=" not in p:
                return None
            k, v = p.split("=", 1)
            args2[k.strip()] = _parse_value(v.strip())
        return name, args2

    # tool_name key=value ...
    m2 = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s+(.*)$", t)
    if m2:
        name = m2.group(1)
        rest = m2.group(2).strip()
        args3: Dict[str, Any] = {}
        for tok in rest.split():
            if "=" not in tok:
                return None
            k, v = tok.split("=", 1)
            args3[k.strip()] = _parse_value(v.strip())
        return name, args3

    return None


def _allowed_tool_names(tools_schema: List[Dict[str, Any]]) -> set[str]:
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


class OllamaBackend(LLMBackend):
    """
    Ollama backend (local LLM) using HTTP.

    Env vars:
      - OLLAMA_BASE_URL (default: http://127.0.0.1:11434)
      - OLLAMA_MODEL (default: llama3.2:3b)
      - OLLAMA_TIMEOUT_SEC (default: 120)
      - OLLAMA_TOOL_TEMP_ZERO (default: 1)  -> force temperature=0 when tools are present
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
        self.tool_temp_zero = _env_bool("OLLAMA_TOOL_TEMP_ZERO", default=True)

    def step(self, request: LLMRequest) -> LLMResponse:
        url = f"{self.base_url}/api/chat"

        # Deterministic preference when tools exist
        temp = float(request.temperature)
        if self.tool_temp_zero and (request.tools or []):
            temp = 0.0

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": request.messages,
            "stream": False,
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

        if 500 <= r.status_code <= 599:
            raise TransientAPIError(f"Ollama server error {r.status_code}: {r.text}")
        if 400 <= r.status_code <= 499:
            raise PermanentAPIError(f"Ollama request error {r.status_code}: {r.text}")

        try:
            data = r.json()
        except json.JSONDecodeError as e:
            raise TransientAPIError(f"Ollama returned non-JSON response: {e}") from e

        msg = data.get("message") or {}

        # 1) Structured tool_calls (preferred)
        tool_calls: List[ToolCall] = []
        raw_tool_calls = msg.get("tool_calls") or []
        for tc in raw_tool_calls:
            fn = (tc.get("function") or {})
            name = fn.get("name")
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {"_raw": args}
            if isinstance(name, str) and name:
                tool_calls.append(ToolCall(name=name, args=args or {}))

        # Usage (optional)
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
            return LLMResponse(type="tool_call", tool_calls=tool_calls, content=None, reasoning=None, usage=usage)

        # 2) Deterministic fallback: parse tool call from content (only if tool is allowed)
        content = msg.get("content")
        content_str = content if isinstance(content, str) else ""
        allowed = _allowed_tool_names(request.tools)

        parsed = _try_parse_single_tool_call(content_str)
        if parsed is not None:
            name, args = parsed
            if name in allowed:
                return LLMResponse(
                    type="tool_call",
                    tool_calls=[ToolCall(name=name, args=args)],
                    content=None,
                    reasoning=None,
                    usage=usage,
                )

        # 3) Final
        return LLMResponse(type="final", tool_calls=[], content=content_str, reasoning=None, usage=usage)