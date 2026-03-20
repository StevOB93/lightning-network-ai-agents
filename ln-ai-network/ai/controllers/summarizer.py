from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ai.controllers.shared import _env_float, _env_int
from ai.llm.base import LLMBackend, LLMError, LLMRequest
from ai.models import IntentBlock, StepResult


# =============================================================================
# Error
# =============================================================================

class SummarizerError(Exception):
    """
    Raised when the Summarizer LLM call fails outright (network error, etc.).
    In practice this exception is never propagated — the pipeline coordinator
    catches all summarizer exceptions and falls back to intent.human_summary.
    """


# =============================================================================
# Config
# =============================================================================

@dataclass(frozen=True)
class SummarizerConfig:
    """
    Immutable configuration for the Summarizer LLM call.

    max_output_tokens: 512 is sufficient for a 1-4 sentence summary.
    temperature: Slightly higher than translator/planner (0.2) to allow
      more natural language expression in the answer.
    """
    max_output_tokens: int = 512
    temperature: float = 0.2

    @staticmethod
    def from_env() -> SummarizerConfig:
        return SummarizerConfig(
            max_output_tokens=_env_int("SUMMARIZER_MAX_OUTPUT_TOKENS", 512),
            temperature=_env_float("SUMMARIZER_TEMPERATURE", 0.2),
        )


# =============================================================================
# Summarizer
# =============================================================================

# System prompt for the summarizer. Instructs the LLM to produce a concise
# plain-text answer incorporating actual data from the tool results.
# No JSON, no markdown — just the answer text that goes into the summary card.
_SYSTEM_PROMPT = """\
You are a concise answer generator for a Lightning Network agent running in regtest.

Given the user's original request and the raw results from tool executions,
produce a short, human-readable answer that incorporates the actual data from
the tool results.

Rules:
- Output ONLY the answer text — no JSON, no markdown fences, no explanation of your process.
- Include specific numbers, balances, node IDs, channel states, or other concrete data from the results.
- Keep it concise: 1-4 sentences.
- If a tool returned an error, mention what failed.
"""


class Summarizer:
    """
    Stage 4 of the pipeline: converts raw MCP tool results into a
    human-readable answer using a single LLM call.

    This is the "last mile" of the pipeline — it receives the raw JSON payloads
    from all executed steps and distils them into plain English that can be
    displayed in the UI summary card.

    Unlike the other stages, the Summarizer has no retry loop and no strict
    output format to validate. If the LLM call fails for any reason, the
    pipeline falls back to the Translator's pre-computed human_summary
    (which is always present and valid).

    No retry loop: if the LLM fails here, the fallback is immediate and
    non-disruptive. A retry would add latency at the tail of an already-
    completed execution, with minimal benefit.
    """

    def __init__(
        self,
        config: SummarizerConfig,
        backend: LLMBackend,
        trace: Any,
    ) -> None:
        self.config = config
        self.backend = backend
        self.trace = trace

    def summarize(
        self,
        intent: IntentBlock,
        step_results: List[StepResult],
        req_id: int,
    ) -> str:
        """
        Produce a human-readable summary from intent + tool results.

        The user message contains:
          - user_request: the original prompt verbatim
          - goal: the structured one-line goal from the intent
          - results: list of {tool, ok, raw_result} for each executed step

        Including raw_result gives the LLM access to specific numbers
        (balances, channel capacities, node IDs) to include in the answer.

        Falls back to intent.human_summary on any LLM error.
        """
        # Build the user message as structured JSON for deterministic parsing by the LLM
        user_content = json.dumps({
            "user_request": intent.raw_prompt,
            "goal": intent.goal,
            "results": [
                {
                    "tool": r.tool,
                    "ok": r.ok,
                    "raw_result": r.raw_result,
                }
                for r in step_results
            ],
        }, ensure_ascii=False, indent=2)

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

        self.trace.log({
            "event": "llm_call",
            "stage": "summarizer",
            "req_id": req_id,
        })

        try:
            req = LLMRequest(
                messages=messages,
                tools=[],  # Pure text generation — no tool calls
                max_output_tokens=self.config.max_output_tokens,
                temperature=self.config.temperature,
            )
            resp = self.backend.step(req)
        except LLMError as e:
            # Log the error and fall back to the static human_summary from the intent
            self.trace.log({
                "event": "llm_error",
                "stage": "summarizer",
                "req_id": req_id,
                "error": str(e),
            })
            return intent.human_summary

        content = (resp.content or "").strip()
        self.trace.log({
            "event": "llm_response",
            "stage": "summarizer",
            "req_id": req_id,
            "content_preview": content[:300],
        })

        # Return LLM output if non-empty, otherwise fall back to static summary
        return content if content else intent.human_summary
