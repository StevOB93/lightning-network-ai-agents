from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ai.intent_validate import validate_intent_safety
from ai.llm.base import LLMBackend, LLMError, LLMRequest
from ai.models import IntentBlock


# =============================================================================
# Error
# =============================================================================

class TranslatorError(Exception):
    """Raised when the Translator cannot produce a valid IntentBlock."""


# =============================================================================
# Config
# =============================================================================

_VALID_INTENT_TYPES = {
    "open_channel", "set_fee", "rebalance", "pay_invoice", "noop", "freeform",
}


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    return default if v is None or v.strip() == "" else int(v)


def _env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    return default if v is None or v.strip() == "" else float(v)


@dataclass(frozen=True)
class TranslatorConfig:
    max_output_tokens: int = 512
    temperature: float = 0.1
    max_retries: int = 2

    @staticmethod
    def from_env() -> TranslatorConfig:
        return TranslatorConfig(
            max_output_tokens=_env_int("TRANSLATOR_MAX_OUTPUT_TOKENS", 512),
            temperature=_env_float("TRANSLATOR_TEMPERATURE", 0.1),
            max_retries=_env_int("TRANSLATOR_MAX_RETRIES", 2),
        )


# =============================================================================
# Translator
# =============================================================================

_SYSTEM_PROMPT = """\
You are a Lightning Network intent parser.

Given a user's natural language request, extract their intent and return it as a
single JSON object. Output ONLY the JSON — no markdown fences, no explanation text.

The JSON must have exactly these fields:
{
  "goal": "<one machine-readable sentence describing what the user wants>",
  "intent_type": "<one of: open_channel | set_fee | rebalance | pay_invoice | noop | freeform>",
  "context": {
    "<entity_name>": <value>
  },
  "success_criteria": ["<criterion 1>", "<criterion 2>"],
  "clarifications_needed": [],
  "human_summary": "<friendly confirmation of what you understood, 1-2 sentences>"
}

Intent type guide:
- open_channel: user wants to open a payment channel between nodes
- set_fee: user wants to change fee policy on a channel
- rebalance: user wants to move liquidity between channels
- pay_invoice: user wants to pay a BOLT11 invoice or send a payment
- noop: nothing actionable — status check, question, or unclear
- freeform: any other actionable request that doesn't fit the above

Context should include any extracted values relevant to the intent:
- node numbers (e.g., "from_node": 1, "to_node": 2)
- amounts in sat or msat (e.g., "amount_sat": 100000)
- labels, descriptions, bolt11 strings if present

If the intent is unambiguous, leave "clarifications_needed" empty.
If uncertain, set intent_type to "noop" and explain in "goal".
"""


def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences if the LLM wrapped the JSON in them."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


class Translator:
    def __init__(
        self,
        config: TranslatorConfig,
        backend: LLMBackend,
        trace: Any,
    ) -> None:
        self.config = config
        self.backend = backend
        self.trace = trace  # TraceLogger instance

    def translate(
        self,
        raw_prompt: str,
        req_id: int,
        history: Optional[List[Dict[str, Any]]] = None,
    ) -> IntentBlock:
        """
        Parse raw_prompt into a structured IntentBlock via a single LLM call.
        Retries up to config.max_retries times on JSON parse failure.
        Raises TranslatorError if all attempts fail.

        history: optional list of prior {"role": "user"|"assistant", "content": str}
                 dicts providing conversation context for follow-up prompts.
        """
        messages: List[Dict[str, Any]] = [{"role": "system", "content": _SYSTEM_PROMPT}]
        for turn in (history or []):
            messages.append({"role": turn["role"], "content": turn["content"]})
        messages.append({"role": "user", "content": raw_prompt})

        last_error: str = "unknown"
        for attempt in range(1, self.config.max_retries + 2):
            self.trace.log({
                "event": "llm_call",
                "stage": "translator",
                "req_id": req_id,
                "attempt": attempt,
            })

            try:
                req = LLMRequest(
                    messages=messages,
                    tools=[],
                    max_output_tokens=self.config.max_output_tokens,
                    temperature=self.config.temperature,
                )
                resp = self.backend.step(req)
            except LLMError as e:
                raise TranslatorError(f"LLM error during translation: {e}") from e

            content = (resp.content or "").strip()
            self.trace.log({
                "event": "llm_response",
                "stage": "translator",
                "req_id": req_id,
                "attempt": attempt,
                "content_preview": content[:300],
            })

            try:
                intent = self._parse_intent_block(content, raw_prompt)
                self.trace.log({
                    "event": "intent_parsed",
                    "stage": "translator",
                    "req_id": req_id,
                    "intent": intent.to_dict(),
                })
                return intent
            except (ValueError, KeyError) as e:
                last_error = str(e)
                self.trace.log({
                    "event": "parse_failed",
                    "stage": "translator",
                    "req_id": req_id,
                    "attempt": attempt,
                    "error": last_error,
                })
                if attempt <= self.config.max_retries:
                    messages.append({"role": "assistant", "content": content})
                    messages.append({
                        "role": "user",
                        "content": (
                            f"Your response could not be parsed as a valid IntentBlock JSON. "
                            f"Error: {last_error}\n"
                            f"Please try again. Output ONLY the JSON object, no markdown."
                        ),
                    })

        raise TranslatorError(
            f"Translator failed after {self.config.max_retries + 1} attempts. "
            f"Last error: {last_error}"
        )

    def _parse_intent_block(self, content: str, raw_prompt: str) -> IntentBlock:
        """Parse LLM content into an IntentBlock. Raises ValueError on bad input."""
        cleaned = _strip_code_fences(content)

        try:
            obj = json.loads(cleaned)
        except json.JSONDecodeError as e:
            raise ValueError(f"Not valid JSON: {e}") from e

        if not isinstance(obj, dict):
            raise ValueError("Expected a JSON object")

        goal = obj.get("goal", "").strip()
        if not goal:
            raise ValueError("Missing or empty 'goal' field")

        intent_type = str(obj.get("intent_type", "freeform")).strip().lower()
        if intent_type not in _VALID_INTENT_TYPES:
            intent_type = "freeform"

        context = obj.get("context", {})
        if not isinstance(context, dict):
            context = {}

        success_criteria = obj.get("success_criteria", [])
        if not isinstance(success_criteria, list):
            success_criteria = []

        clarifications_needed = obj.get("clarifications_needed", [])
        if not isinstance(clarifications_needed, list):
            clarifications_needed = []

        human_summary = str(obj.get("human_summary", goal)).strip()

        intent = IntentBlock(
            goal=goal,
            intent_type=intent_type,
            context={str(k): v for k, v in context.items()},
            success_criteria=[str(c) for c in success_criteria],
            clarifications_needed=[str(c) for c in clarifications_needed],
            human_summary=human_summary,
            raw_prompt=raw_prompt,
        )

        # Safety gate
        ok, reason = validate_intent_safety({"intent": intent.intent_type, **intent.context})
        if not ok:
            raise ValueError(f"Intent failed safety check: {reason}")

        return intent
