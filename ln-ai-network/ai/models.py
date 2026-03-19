from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# =============================================================================
# IntentBlock — output of the Translator
# =============================================================================

@dataclass(frozen=True)
class IntentBlock:
    """Structured representation of what the user wants."""
    goal: str                          # Machine-readable one-sentence goal
    intent_type: str                   # Enum: open_channel|set_fee|rebalance|pay_invoice|noop|freeform
    context: Dict[str, Any]            # Extracted entities (node ids, amounts, labels, etc.)
    success_criteria: List[str]        # What "done" looks like (for executor verification)
    clarifications_needed: List[str]   # Empty = unambiguous intent
    human_summary: str                 # Friendly readable confirmation to show the user
    raw_prompt: str                    # Original user text (preserved for trace)

    def to_dict(self) -> Dict[str, Any]:
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
    """A single tool call in the execution plan."""
    step_id: int                       # 1-based sequential ID
    tool: str                          # MCP tool name
    args: Dict[str, Any]               # Args (may contain "$stepN.result.payload.field" placeholders)
    expected_outcome: str              # What success looks like for this step
    depends_on: List[int]              # step_ids that must complete first (empty = no deps)
    on_error: str                      # "abort" | "retry" | "skip"
    max_retries: int                   # Retries if on_error="retry"; 0 = no retry

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
        return PlanStep(
            step_id=int(d["step_id"]),
            tool=str(d["tool"]),
            args=dict(d.get("args", {})),
            expected_outcome=str(d.get("expected_outcome", "")),
            depends_on=[int(x) for x in d.get("depends_on", [])],
            on_error=str(d.get("on_error", "abort")),
            max_retries=int(d.get("max_retries", 0)),
        )


# =============================================================================
# ExecutionPlan — output of the Planner
# =============================================================================

@dataclass
class ExecutionPlan:
    """Ordered sequence of PlanSteps to fulfill an IntentBlock."""
    steps: List[PlanStep]
    plan_rationale: str                # LLM's explanation of the plan
    intent: IntentBlock                # Back-reference to the intent being fulfilled

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
            intent=IntentBlock.from_dict(d["intent"]),
        )


# =============================================================================
# StepResult — result of executing one PlanStep
# =============================================================================

@dataclass
class StepResult:
    """Result of executing a single PlanStep."""
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
            step_id=int(d["step_id"]),
            tool=str(d["tool"]),
            args=dict(d.get("args", {})),
            ok=bool(d.get("ok", False)),
            error=d.get("error"),
            raw_result=d.get("raw_result"),
            retries_used=int(d.get("retries_used", 0)),
            skipped=bool(d.get("skipped", False)),
        )


# =============================================================================
# PipelineResult — final output of the full 3-stage pipeline
# =============================================================================

@dataclass
class PipelineResult:
    """Complete result of one pipeline run (translator → planner → executor)."""
    request_id: int
    ts: int
    success: bool
    stage_failed: Optional[str]        # "translator" | "planner" | "executor" | None
    intent: Optional[IntentBlock]
    plan: Optional[ExecutionPlan]
    step_results: List[StepResult]
    human_summary: str                 # Final readable answer for the user
    error: Optional[str]
    pipeline_build: str

    def to_outbox_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "type": "pipeline_report",
            "request_id": self.request_id,
            "success": self.success,
            "stage_failed": self.stage_failed,
            "intent": self.intent.to_dict() if self.intent else None,
            "plan": self.plan.to_dict() if self.plan else None,
            "step_results": [r.to_dict() for r in self.step_results],
            "content": self.human_summary,
            "error": self.error,
            "pipeline_build": self.pipeline_build,
        }
