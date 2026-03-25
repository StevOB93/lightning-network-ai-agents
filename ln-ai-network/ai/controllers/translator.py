from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ai.controllers.shared import (
    _env_float,
    _env_int,
    _get_node_count,
    _repair_json,
    _strip_code_fences,
)
from ai.intent_validate import validate_intent_safety
from ai.llm.base import LLMBackend, LLMError, LLMRequest
from ai.models import IntentBlock


# =============================================================================
# Error
# =============================================================================

class TranslatorError(Exception):
    """
    Raised when the Translator cannot produce a valid IntentBlock after all
    retry attempts are exhausted. The message includes the attempt count and
    the last parse error for debugging.
    """


# =============================================================================
# Config
# =============================================================================

# All intent types the system understands. The LLM is instructed to use exactly
# one of these strings. Any unrecognised value is coerced to "freeform" in
# _parse_intent_block so a hallucinated type doesn't cause a hard failure.
_VALID_INTENT_TYPES = {
    "open_channel", "set_fee", "rebalance", "pay_invoice", "noop", "freeform",
    "recall",  # user is asking about past operations / run history
}


@dataclass(frozen=True)
class TranslatorConfig:
    """
    Immutable configuration for the Translator LLM call.

    max_output_tokens: Caps the JSON response length. 512 is generous for the
      structured intent JSON — keeping it low reduces cost and latency.
    temperature: Low (0.1) for deterministic structured extraction.
      Higher values risk creative hallucination of node IDs or amounts.
    max_retries: How many additional attempts after the first parse failure.
      Each retry appends the error to the conversation so the LLM can self-correct.
    """
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

# System prompt template for the translator LLM call.
# Uses Python's .format() so {node_count} is injected at runtime.
# Double braces {{ }} are literal braces in the output (not format fields).
_SYSTEM_PROMPT_TMPL = """\
You are a Lightning Network intent parser running in regtest.
Available nodes: 1 through {node_count}.

Given a user's natural language request, extract their intent and return it as a
single JSON object. Output ONLY the JSON — no markdown fences, no explanation text.

The JSON must have exactly these fields:
{{
  "goal": "<one machine-readable sentence describing what the user wants>",
  "intent_type": "<one of: open_channel | set_fee | rebalance | pay_invoice | recall | noop | freeform>",
  "context": {{
    "<entity_name>": <value>
  }},
  "success_criteria": ["<criterion 1>", "<criterion 2>"],
  "clarifications_needed": [],
  "human_summary": "<friendly confirmation of what you understood, 1-2 sentences>"
}}

Intent type guide:
- open_channel: user wants to open a payment channel between nodes
- set_fee: user wants to change fee policy on a channel
- rebalance: user wants to move liquidity between channels
- pay_invoice: user wants to pay a BOLT11 invoice or send a specific payment
- recall: user is asking about past operations or run history ("what did I run last time?",
  "did the payment succeed?", "show recent history", "what happened before?", "last run")
- noop: greeting, unclear request, meta-question about the agent, or anything with no actionable intent
- freeform: any other actionable request including balance checks, diagnostic tests, status queries, node info, mining, or anything that needs tool calls

IMPORTANT:
- Only include values in context that the user EXPLICITLY stated. Do NOT invent
  node numbers, amounts, invoice IDs, or any other values.
- Node numbers must be between 1 and {node_count}.
- Use "noop" ONLY when there is truly nothing to do. If the user wants information
  or wants to run any check/test, use "freeform" instead.

Context should include any extracted values relevant to the intent:
- node numbers (e.g., "from_node": 1, "to_node": 2)
- amounts in sat or msat (e.g., "amount_sat": 100000)
- labels, descriptions, bolt11 strings if present

If the intent is unambiguous, leave "clarifications_needed" empty.
If uncertain, set intent_type to "noop" and explain in "goal".
"""




class Translator:
    """
    Stage 1 of the pipeline: converts a raw natural language prompt into a
    structured IntentBlock using a single LLM call.

    Retry logic: on JSON parse failure the error message is appended to the
    conversation as a user turn so the LLM can see its own mistake and try
    again. This self-correction approach works well for minor formatting errors
    but usually fails if the LLM fundamentally misunderstood the task.
    """

    def __init__(
        self,
        config: TranslatorConfig,
        backend: LLMBackend,
        trace: Any,
    ) -> None:
        self.config = config
        self.backend = backend
        self.trace = trace  # TraceLogger shared with all pipeline stages

    def translate(
        self,
        raw_prompt: str,
        req_id: int,
        history: Optional[List[Dict[str, Any]]] = None,
    ) -> IntentBlock:
        """
        Parse raw_prompt into a structured IntentBlock via an LLM call.

        The message list is constructed as:
          [system] → [history turns...] → [user: raw_prompt]

        Including history lets follow-up queries like "now do the same for node 3"
        resolve the referent ("same") from the prior assistant turn.

        Retries up to config.max_retries on JSON parse failure.
        Raises TranslatorError if all attempts fail.
        """
        system_prompt = _SYSTEM_PROMPT_TMPL.format(node_count=_get_node_count())
        messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]

        # Inject prior conversation turns for context continuity
        for turn in (history or []):
            messages.append({"role": turn["role"], "content": turn["content"]})
        messages.append({"role": "user", "content": raw_prompt})

        last_error: str = "unknown"
        # Range produces attempt numbers 1, 2, ..., max_retries+1
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
                    tools=[],  # No tool calls — pure text completion
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
                "content_preview": content[:600],  # Truncated to keep trace compact
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
                # Feed the error back to the LLM for self-correction on next attempt
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
        """
        Parse and validate LLM content into an IntentBlock.

        Raises ValueError on any structural problem so the retry loop can
        catch it uniformly.

        Validation steps:
          1. Strip code fences and repair JSON formatting
          2. Parse JSON — raises ValueError on decode failure
          3. Validate required fields (goal is mandatory)
          4. Coerce intent_type to a known value (default: freeform)
          5. Strip context values with node numbers outside [1, node_count]
             (LLMs sometimes hallucinate node IDs like 3 in a 2-node network)
          6. Run the intent safety gate (validate_intent_safety)
        """
        cleaned = _strip_code_fences(content)
        cleaned = _repair_json(cleaned)

        try:
            obj = json.loads(cleaned)
        except json.JSONDecodeError as e:
            # Include the 40-char window around the failure so the trace log
            # shows exactly what character caused the parse error.
            lo = max(0, e.pos - 20)
            hi = min(len(cleaned), e.pos + 20)
            raise ValueError(
                f"Not valid JSON: {e} | context: {repr(cleaned[lo:hi])}"
            ) from e

        if not isinstance(obj, dict):
            raise ValueError("Expected a JSON object")

        goal = obj.get("goal", "").strip()
        if not goal:
            raise ValueError("Missing or empty 'goal' field")

        # Coerce unrecognised intent types to "freeform" rather than hard-failing.
        # This handles LLM outputs like "query" or "diagnostic".
        intent_type = str(obj.get("intent_type", "freeform")).strip().lower()
        if intent_type not in _VALID_INTENT_TYPES:
            intent_type = "freeform"

        # Defensively default all collection fields to empty containers
        context = obj.get("context", {})
        if not isinstance(context, dict):
            context = {}

        success_criteria = obj.get("success_criteria", [])
        if not isinstance(success_criteria, list):
            success_criteria = []

        clarifications_needed = obj.get("clarifications_needed", [])
        if not isinstance(clarifications_needed, list):
            clarifications_needed = []

        # Fall back to goal if human_summary was omitted
        human_summary = str(obj.get("human_summary", goal)).strip()

        # Safety: strip fabricated node numbers that are outside the valid range.
        # The LLM sometimes invents node 3 when only nodes 1 and 2 exist.
        node_count = _get_node_count()
        for key in ("node", "from_node", "to_node"):
            val = context.get(key)
            if isinstance(val, (int, float)) and (val < 1 or val > node_count):
                context.pop(key)

        intent = IntentBlock(
            goal=goal,
            intent_type=intent_type,
            # Stringify all context keys (LLM may emit integer keys)
            context={str(k): v for k, v in context.items()},
            success_criteria=[str(c) for c in success_criteria],
            clarifications_needed=[str(c) for c in clarifications_needed],
            human_summary=human_summary,
            raw_prompt=raw_prompt,
        )

        # Final safety gate: validate against intent_validate rules.
        # This catches dangerous operations like invoicing absurd amounts
        # or targeting non-existent nodes.
        ok, reason = validate_intent_safety({"intent_type": intent.intent_type, **intent.context})
        if not ok:
            raise ValueError(f"Intent failed safety check: {reason}")

        return intent
