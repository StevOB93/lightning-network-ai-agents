import json
import os
from typing import Any, Callable, List

from ai.llm.base import LLMBackend


class GeminiBackend(LLMBackend):
    def __init__(self) -> None:
        try:
            from google import genai
            from google.genai import types
        except ImportError as e:
            raise RuntimeError(
                "The 'google-genai' Python package is not installed. Run 'pip install -r requirements.txt'."
            ) from e

        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not set")

        timeout_s = float(os.getenv("GEMINI_TIMEOUT_S", "180"))

        self._genai = genai
        self._types = types
        self.client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=int(timeout_s * 1000)),
        )
        self.model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    def step(self, messages: List[dict[str, Any]], tools: List[dict[str, Any]]) -> dict[str, Any]:
        raise RuntimeError("GeminiBackend.step is not used. Call run_prompt instead.")

    def run_prompt(
        self,
        *,
        system_prompt: str,
        user_text: str,
        tool_functions: List[Callable[..., Any]],
    ) -> dict[str, Any]:
        types = self._types

        config = types.GenerateContentConfig(
            systemInstruction=system_prompt,
            tools=tool_functions,
            temperature=0.2,
            automaticFunctionCalling=types.AutomaticFunctionCallingConfig(disable=False),
        )

        response = self.client.models.generate_content(
            model=self.model,
            contents=user_text,
            config=config,
        )

        content = response.text
        if content:
            return {"type": "final", "content": content}

        return {
            "type": "final",
            "content": json.dumps(response.model_dump(mode="json", exclude_none=True), ensure_ascii=False),
        }
