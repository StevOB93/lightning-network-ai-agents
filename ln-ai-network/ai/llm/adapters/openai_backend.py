import json
import os
from typing import List, Dict, Any

from ai.llm.base import LLMBackend


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

    def __init__(self) -> None:
        try:
            from openai import OpenAI
        except ImportError as e:
            raise RuntimeError(
                "The 'openai' Python package is not installed. Run 'pip install -r requirements.txt'."
            ) from e

        provider = os.getenv("LLM_PROVIDER", "gemini").strip().lower()

        if provider == "gemini":
            api_key = os.getenv("GEMINI_API_KEY")
            if not api_key:
                raise RuntimeError("GEMINI_API_KEY not set")

            base_url = os.getenv(
                "GEMINI_BASE_URL",
                "https://generativelanguage.googleapis.com/v1beta/openai/",
            )
            model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
            timeout_s = float(os.getenv("GEMINI_TIMEOUT_S", "60"))
        else:
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                raise RuntimeError("OPENAI_API_KEY not set")

            base_url = os.getenv("OPENAI_BASE_URL")
            model = os.getenv("OPENAI_MODEL", "gpt-4o")
            timeout_s = float(os.getenv("OPENAI_TIMEOUT_S", "60"))

        client_kwargs: Dict[str, Any] = {
            "api_key": api_key,
            "timeout": timeout_s,
        }
        if base_url:
            client_kwargs["base_url"] = base_url

        self.client = OpenAI(**client_kwargs)
        self.model = model

    def step(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        request: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
        }
        if tools:
            request["tools"] = tools
            # Gemini's OpenAI-compatible function calling examples specify tool_choice.
            request["tool_choice"] = "auto"

        response = self.client.chat.completions.create(**request)

        choice = response.choices[0]

        if choice.finish_reason == "tool_calls":
            tool_call = choice.message.tool_calls[0]
            return {
                "type": "tool_call",
                "tool_name": tool_call.function.name,
                "tool_args": _parse_args(tool_call.function.arguments),
                "content": None,
                "reasoning": choice.message.content,
            }

        return {
            "type": "final",
            "tool_name": None,
            "tool_args": None,
            "content": choice.message.content,
            "reasoning": None,
        }
