import json
import os
from typing import List, Dict, Any

from openai import OpenAI
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
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set")

        self.client = OpenAI(api_key=api_key)
        self.model = os.getenv("OPENAI_MODEL", "gpt-4o")

    def step(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> Dict[str, Any]:

        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            temperature=0.2,
        )

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