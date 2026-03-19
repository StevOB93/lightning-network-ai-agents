from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ai.llm.base import LLMBackend, LLMError, LLMRequest
from ai.models import ExecutionPlan, IntentBlock, PlanStep
from ai.tools import TOOL_REQUIRED, llm_tools_schema_text


# =============================================================================
# Error
# =============================================================================

class PlannerError(Exception):
    """Raised when the Planner cannot produce a valid ExecutionPlan."""


# =============================================================================
# Config
# =============================================================================

_VALID_ON_ERROR = {"abort", "retry", "skip"}


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    return default if v is None or v.strip() == "" else int(v)


def _env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    return default if v is None or v.strip() == "" else float(v)


@dataclass(frozen=True)
class PlannerConfig:
    max_output_tokens: int = 1024
    temperature: float = 0.1
    max_retries: int = 2

    @staticmethod
    def from_env() -> PlannerConfig:
        return PlannerConfig(
            max_output_tokens=_env_int("PLANNER_MAX_OUTPUT_TOKENS", 1024),
            temperature=_env_float("PLANNER_TEMPERATURE", 0.1),
            max_retries=_env_int("PLANNER_MAX_RETRIES", 2),
        )


# =============================================================================
# Planner
# =============================================================================

_SYSTEM_PROMPT_TMPL = """\
You are a Lightning Network execution planner.

Given a structured intent (JSON), produce a minimal ordered list of MCP tool calls
that fulfill it. Output ONLY a single JSON object — no markdown fences, no explanation.

The JSON must have exactly these fields:
{{
  "plan_rationale": "<1-2 sentence explanation of the plan>",
  "steps": [
    {{
      "step_id": <1-based integer>,
      "tool": "<tool name>",
      "args": {{<tool arguments>}},
      "expected_outcome": "<what success looks like for this step>",
      "depends_on": [<step_ids that must complete first, or empty list>],
      "on_error": "<abort|retry|skip>",
      "max_retries": <integer, 0 unless on_error is retry>
    }}
  ]
}}

Rules:
- Use ONLY the tools listed below. Do not invent tool names.
- All required args must be present. For args not yet known (e.g. a bolt11 from a prior step),
  use a placeholder like "$step1.result.payload.bolt11".
- Use "abort" for critical steps, "skip" for optional reads, "retry" only if retrying makes sense.
- Produce the minimal set of steps — no extra reads unless necessary.
- If the intent is "noop" or has no clear tool path, return "steps": [].

{tools_schema}"""


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _validate_plan_steps(steps: List[Dict[str, Any]]) -> Optional[str]:
    """Validate raw step dicts from LLM output. Returns error string or None."""
    seen_ids: set = set()
    for i, s in enumerate(steps):
        step_id = s.get("step_id")
        if not isinstance(step_id, int) or isinstance(step_id, bool) or step_id < 1:
            return f"step[{i}] has invalid step_id: {step_id!r}"
        if step_id in seen_ids:
            return f"duplicate step_id: {step_id}"
        seen_ids.add(step_id)

        tool = s.get("tool", "")
        if tool not in TOOL_REQUIRED:
            return f"step {step_id}: unknown tool '{tool}'"

        args = s.get("args", {})
        if not isinstance(args, dict):
            return f"step {step_id}: args must be a dict"

        for req_key in TOOL_REQUIRED.get(tool, []):
            if req_key not in args:
                return f"step {step_id}: tool '{tool}' missing required arg '{req_key}'"

        on_error = s.get("on_error", "abort")
        if on_error not in _VALID_ON_ERROR:
            return f"step {step_id}: invalid on_error '{on_error}'"

    return None


class Planner:
    def __init__(
        self,
        config: PlannerConfig,
        backend: LLMBackend,
        trace: Any,
    ) -> None:
        self.config = config
        self.backend = backend
        self.trace = trace

    def plan(self, intent: IntentBlock, req_id: int) -> ExecutionPlan:
        """
        Produce an ExecutionPlan from an IntentBlock via a single LLM call.
        Retries up to config.max_retries on parse/validation failure.
        Raises PlannerError if all attempts fail.
        """
        system_prompt = _SYSTEM_PROMPT_TMPL.format(tools_schema=llm_tools_schema_text())
        user_content = json.dumps(intent.to_dict(), ensure_ascii=False, indent=2)

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        last_error: str = "unknown"
        for attempt in range(1, self.config.max_retries + 2):
            self.trace.log({
                "event": "llm_call",
                "stage": "planner",
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
                raise PlannerError(f"LLM error during planning: {e}") from e

            content = (resp.content or "").strip()
            self.trace.log({
                "event": "llm_response",
                "stage": "planner",
                "req_id": req_id,
                "attempt": attempt,
                "content_preview": content[:300],
            })

            try:
                plan = self._parse_plan(content, intent)
                self.trace.log({
                    "event": "plan_parsed",
                    "stage": "planner",
                    "req_id": req_id,
                    "steps": len(plan.steps),
                    "rationale_preview": plan.plan_rationale[:200],
                })
                return plan
            except (ValueError, KeyError) as e:
                last_error = str(e)
                self.trace.log({
                    "event": "parse_failed",
                    "stage": "planner",
                    "req_id": req_id,
                    "attempt": attempt,
                    "error": last_error,
                })
                if attempt <= self.config.max_retries:
                    messages.append({"role": "assistant", "content": content})
                    messages.append({
                        "role": "user",
                        "content": (
                            f"Your response could not be parsed as a valid ExecutionPlan JSON. "
                            f"Error: {last_error}\n"
                            f"Please try again. Output ONLY the JSON object, no markdown."
                        ),
                    })

        raise PlannerError(
            f"Planner failed after {self.config.max_retries + 1} attempts. "
            f"Last error: {last_error}"
        )

    def _parse_plan(self, content: str, intent: IntentBlock) -> ExecutionPlan:
        cleaned = _strip_code_fences(content)

        try:
            obj = json.loads(cleaned)
        except json.JSONDecodeError as e:
            raise ValueError(f"Not valid JSON: {e}") from e

        if not isinstance(obj, dict):
            raise ValueError("Expected a JSON object")

        rationale = str(obj.get("plan_rationale", "")).strip()

        steps_raw = obj.get("steps", [])
        if not isinstance(steps_raw, list):
            raise ValueError("'steps' must be a list")

        err = _validate_plan_steps(steps_raw)
        if err:
            raise ValueError(f"Plan validation failed: {err}")

        steps = [PlanStep.from_dict(s) for s in steps_raw]
        return ExecutionPlan(steps=steps, plan_rationale=rationale, intent=intent)
