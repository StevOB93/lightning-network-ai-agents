from __future__ import annotations

# =============================================================================
# Data models — shared types used across all pipeline stages
#
# Data flow through the 4-stage pipeline:
#
#   User prompt (str)
#       │  Translator
#       ▼
#   IntentBlock (frozen dataclass)   ← structured intent with goal, type, context
#       │  Planner
#       ▼
#   ExecutionPlan (mutable dataclass) ← ordered list of PlanStep objects
#       │  Executor
#       ▼
#   List[StepResult]                  ← one result per step (ok/fail/skip)
#       │  Summarizer + PipelineCoordinator
#       ▼
#   PipelineResult                    ← full run record written to outbox
#
# Immutability:
#   IntentBlock and PlanStep use frozen=True — they're created by the Translator
#   and Planner respectively and must not be mutated by downstream stages.
#   ExecutionPlan and StepResult are mutable (dataclass without frozen) to allow
#   modification during plan execution without the overhead of full copies.
# =============================================================================

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


# =============================================================================
# IntentBlock — output of the Translator
# =============================================================================

@dataclass(frozen=True)
class IntentBlock:
    """
    Structured representation of what the user wants.

    Produced by the Translator from raw NL text. Frozen so downstream stages
    can safely pass it around without risk of accidental mutation.

    Fields:
      goal              — machine-readable one-sentence goal (e.g. "Open a 500k sat channel")
      intent_type       — coarse category: one of open_channel|set_fee|rebalance|
                          pay_invoice|noop|freeform
      context           — extracted entities from the prompt (node IDs, amounts, labels)
                          Keys are always strings; values may be int, float, str, etc.
      success_criteria  — list of verifiable conditions for "done" (used by goal verification)
      clarifications_needed — empty when intent is unambiguous; non-empty triggers a UI notice
      human_summary     — friendly sentence shown to the user confirming what was understood
      raw_prompt        — original user input preserved verbatim for trace and summarizer context
    """
    goal: str
    intent_type: str
    context: Dict[str, Any]
    success_criteria: List[str]
    clarifications_needed: List[str]
    human_summary: str
    raw_prompt: str

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a JSON-compatible dict (for trace logging and Planner input)."""
        return {
            "goal": self.goal,
            "intent_type": self.intent_type,
            "context": self.context,
            "success_criteria": self.success_criteria,
            "clarifications_needed": self.clarifications_needed,
            "human_summary": self.human_summary,
            "raw_prompt": self.raw_prompt,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> IntentBlock:
        """Deserialize from a dict (e.g. loaded from outbox JSONL for replay)."""
        return IntentBlock(
            goal=d.get("goal", ""),
            intent_type=d.get("intent_type", "freeform"),
            context=d.get("context", {}),
            success_criteria=d.get("success_criteria", []),
            clarifications_needed=d.get("clarifications_needed", []),
            human_summary=d.get("human_summary", ""),
            raw_prompt=d.get("raw_prompt", ""),
        )


# =============================================================================
# PlanStep — one step in an ExecutionPlan
# =============================================================================

@dataclass(frozen=True)
class PlanStep:
    """
    A single MCP tool call in the execution plan.

    Produced by the Planner. Frozen for the same reasons as IntentBlock.

    Fields:
      step_id         — 1-based sequential ID; must be unique within the plan
      tool            — MCP tool name (must be in TOOL_REQUIRED registry)
      args            — tool arguments; may contain "$stepN.path.to.field" placeholders
                        that the Executor resolves at runtime using prior step results
      expected_outcome— human-readable description of what success looks like (for UI)
      depends_on      — step_ids that must complete before this step runs
                        (currently tracked but not enforced — execution is sequential)
      on_error        — error policy: "abort" | "retry" | "skip"
      max_retries     — max extra attempts when on_error="retry" (0 = no retry)
    """
    step_id: int
    tool: str
    args: Dict[str, Any]
    expected_outcome: str
    depends_on: List[int]
    on_error: str
    max_retries: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "tool": self.tool,
            "args": self.args,
            "expected_outcome": self.expected_outcome,
            "depends_on": self.depends_on,
            "on_error": self.on_error,
            "max_retries": self.max_retries,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> PlanStep:
        try:
            depends_on = [int(x) for x in d.get("depends_on", [])]
        except (ValueError, TypeError):
            depends_on = []
        return PlanStep(
            step_id=int(d.get("step_id", 0)),
            tool=str(d.get("tool", "")),
            args=dict(d.get("args", {})),
            expected_outcome=str(d.get("expected_outcome", "")),
            depends_on=depends_on,
            on_error=str(d.get("on_error", "abort")),
            max_retries=int(d.get("max_retries", 0)),
        )


# =============================================================================
# ExecutionPlan — output of the Planner
# =============================================================================

@dataclass
class ExecutionPlan:
    """
    Ordered sequence of PlanSteps to fulfill an IntentBlock.

    Not frozen: the Executor may annotate or inspect the plan in-place,
    and the coordinator may wrap it into a PipelineResult after completion.

    Fields:
      steps           — ordered list of PlanStep objects; Executor runs them in order
      plan_rationale  — LLM's one-or-two-sentence explanation of the plan (shown in UI)
      intent          — back-reference to the IntentBlock being fulfilled;
                        used by the Executor to resolve $context.field placeholders
    """
    steps: List[PlanStep]
    plan_rationale: str
    intent: IntentBlock  # Back-reference for $context.field placeholder resolution

    def to_dict(self) -> Dict[str, Any]:
        return {
            "steps": [s.to_dict() for s in self.steps],
            "plan_rationale": self.plan_rationale,
            "intent": self.intent.to_dict(),
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> ExecutionPlan:
        return ExecutionPlan(
            steps=[PlanStep.from_dict(s) for s in d.get("steps", [])],
            plan_rationale=str(d.get("plan_rationale", "")),
            intent=IntentBlock.from_dict(d.get("intent") or {}),
        )


# =============================================================================
# StepResult — result of executing one PlanStep
# =============================================================================

@dataclass
class StepResult:
    """
    Result of executing a single PlanStep via an MCP tool call.

    Produced by the Executor, consumed by the Summarizer and the pipeline
    coordinator (for the outbox report and archive status determination).

    Fields:
      step_id      — mirrors PlanStep.step_id for correlation
      tool         — tool name (mirrors PlanStep.tool)
      args         — normalized args actually passed to the MCP call
                     (may differ from PlanStep.args after placeholder resolution)
      ok           — True if the tool call succeeded without error
      error        — error message if ok=False (None if ok=True)
      raw_result   — raw JSON dict returned by the MCP tool (used by Summarizer
                     to extract specific numbers, balances, etc.)
      retries_used — number of extra attempts before success or final failure
                     (0 = first attempt succeeded; 1+ = retried at least once)
      skipped      — True when on_error="skip" and the step failed
                     (ok=False, skipped=True means soft failure; ok=True means success)
    """
    step_id: int
    tool: str
    args: Dict[str, Any]
    ok: bool
    error: Optional[str]
    raw_result: Any
    retries_used: int
    skipped: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "tool": self.tool,
            "args": self.args,
            "ok": self.ok,
            "error": self.error,
            "raw_result": self.raw_result,
            "retries_used": self.retries_used,
            "skipped": self.skipped,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> StepResult:
        return StepResult(
            step_id=int(d.get("step_id", 0)),
            tool=str(d.get("tool", "")),
            args=dict(d.get("args", {})),
            ok=bool(d.get("ok", False)),
            error=d.get("error"),
            raw_result=d.get("raw_result"),
            retries_used=int(d.get("retries_used", 0)),
            skipped=bool(d.get("skipped", False)),
        )


# =============================================================================
# PipelineResult — final output of the full pipeline
# =============================================================================

@dataclass
class PipelineResult:
    """
    Complete result of one pipeline run (Translator → Planner → Executor → Summarizer).

    Written to the outbox JSONL by the PipelineCoordinator after each run.
    The UI server reads the outbox and pushes a pipeline_result SSE event to
    connected browsers.

    Fields:
      request_id      — monotonically increasing ID from the inbox message
      ts              — Unix timestamp of when the pipeline completed
      success         — True if all required steps completed without error
      stage_failed    — "translator"|"planner"|"executor"|None — which stage aborted
      intent          — parsed intent (None if translator failed before parsing)
      plan            — execution plan (None if planner failed)
      step_results    — list of all executed step results (partial on executor failure)
      human_summary   — final answer for the user (from Summarizer, or fallback text)
      error           — top-level error message (None on success)
      pipeline_build  — version string embedded in the trace header for debugging
    """
    request_id: int
    ts: int
    success: bool
    stage_failed: Optional[str]
    intent: Optional[IntentBlock]
    plan: Optional[ExecutionPlan]
    step_results: List[StepResult]
    human_summary: str
    error: Optional[str]
    pipeline_build: str

    def to_outbox_dict(self) -> Dict[str, Any]:
        """
        Serialize to the outbox wire format.

        `content` is aliased to `human_summary` for backward compatibility
        with the UI's SSE handler which reads `data.content` for the summary text.
        """
        return {
            "ts": self.ts,
            "type": "pipeline_report",
            "request_id": self.request_id,
            "success": self.success,
            "stage_failed": self.stage_failed,
            "intent": self.intent.to_dict() if self.intent else None,
            "plan": self.plan.to_dict() if self.plan else None,
            "step_results": [r.to_dict() for r in self.step_results],
            "content": self.human_summary,  # UI reads this field
            "error": self.error,
            "pipeline_build": self.pipeline_build,
        }
