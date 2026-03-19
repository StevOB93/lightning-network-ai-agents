from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ai.mcp_client import MCPClient
from ai.models import ExecutionPlan, PlanStep, StepResult
from ai.tools import _is_tool_error, _normalize_tool_args, _summarize_tool_result


# =============================================================================
# Error
# =============================================================================

class ExecutorError(Exception):
    """Raised when a non-skippable step fails during execution."""
    def __init__(self, message: str, partial_results: Optional[List[StepResult]] = None) -> None:
        super().__init__(message)
        self.partial_results: List[StepResult] = partial_results or []


# =============================================================================
# Config
# =============================================================================

@dataclass(frozen=True)
class ExecutorConfig:
    default_on_error: str = "abort"

    @staticmethod
    def from_env() -> ExecutorConfig:
        return ExecutorConfig(
            default_on_error=os.getenv("EXECUTOR_DEFAULT_ON_ERROR", "abort"),
        )


# =============================================================================
# Placeholder resolution
# =============================================================================

# Matches "$stepN.path.to.field"
_PLACEHOLDER_RE = re.compile(r"^\$step(\d+)\.(.+)$")


def _navigate(obj: Any, path: str) -> Any:
    """Navigate a dot-separated path into a nested structure."""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            if part not in cur:
                raise KeyError(f"key '{part}' not found")
            cur = cur[part]
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError) as e:
                raise KeyError(f"list index '{part}': {e}") from e
        else:
            raise KeyError(f"cannot navigate '{part}' in {type(cur).__name__}")
    return cur


def _resolve_value(val: Any, results_by_id: Dict[int, StepResult]) -> Any:
    """Resolve a single value — substitute placeholder if it matches $stepN.path."""
    if not isinstance(val, str):
        return val
    m = _PLACEHOLDER_RE.match(val)
    if not m:
        return val
    step_id = int(m.group(1))
    path = m.group(2)
    if step_id not in results_by_id:
        raise KeyError(
            f"placeholder '{val}' references step {step_id} which has no result yet"
        )
    raw = results_by_id[step_id].raw_result
    try:
        return _navigate(raw, path)
    except (KeyError, TypeError) as e:
        raise KeyError(f"placeholder '{val}': failed to navigate '{path}': {e}") from e


def _resolve_args(args: Dict[str, Any], results_by_id: Dict[int, StepResult]) -> Dict[str, Any]:
    return {k: _resolve_value(v, results_by_id) for k, v in args.items()}


# =============================================================================
# Executor
# =============================================================================

class Executor:
    def __init__(
        self,
        config: ExecutorConfig,
        mcp: MCPClient,
        trace: Any,
    ) -> None:
        self.config = config
        self.mcp = mcp
        self.trace = trace

    def execute(self, plan: ExecutionPlan, req_id: int) -> List[StepResult]:
        """
        Execute all steps in the plan sequentially.
        Returns list of StepResult in execution order.
        Raises ExecutorError if a non-skippable step fails.
        """
        results: List[StepResult] = []
        results_by_id: Dict[int, StepResult] = {}

        for step in plan.steps:
            try:
                result = self._execute_step(step, req_id, results_by_id)
            except ExecutorError as e:
                raise ExecutorError(str(e), partial_results=results) from e
            results.append(result)
            results_by_id[step.step_id] = result

            if not result.ok and not result.skipped:
                raise ExecutorError(
                    f"Step {step.step_id} ({step.tool}) failed: {result.error}",
                    partial_results=results,
                )

        return results

    def _execute_step(
        self,
        step: PlanStep,
        req_id: int,
        results_by_id: Dict[int, StepResult],
    ) -> StepResult:
        self.trace.log({
            "event": "step_start",
            "stage": "executor",
            "req_id": req_id,
            "step_id": step.step_id,
            "tool": step.tool,
        })

        # Resolve placeholders
        try:
            resolved_args = _resolve_args(step.args, results_by_id)
        except KeyError as e:
            err = f"Placeholder resolution failed: {e}"
            self.trace.log({
                "event": "step_placeholder_error",
                "stage": "executor",
                "req_id": req_id,
                "step_id": step.step_id,
                "error": err,
            })
            if step.on_error == "skip":
                return StepResult(
                    step_id=step.step_id, tool=step.tool, args=step.args,
                    ok=False, error=err, raw_result=None, retries_used=0, skipped=True,
                )
            raise ExecutorError(err)

        # Normalize + validate args
        norm_args, norm_err, changed = _normalize_tool_args(step.tool, resolved_args)
        if changed:
            self.trace.log({
                "event": "args_normalized",
                "stage": "executor",
                "req_id": req_id,
                "step_id": step.step_id,
                "before": resolved_args,
                "after": norm_args,
            })
        if norm_err:
            err = f"Invalid tool args: {norm_err}"
            self.trace.log({
                "event": "step_args_error",
                "stage": "executor",
                "req_id": req_id,
                "step_id": step.step_id,
                "error": err,
            })
            if step.on_error == "skip":
                return StepResult(
                    step_id=step.step_id, tool=step.tool, args=norm_args,
                    ok=False, error=err, raw_result=None, retries_used=0, skipped=True,
                )
            raise ExecutorError(err)

        # Execute with retry
        max_attempts = (step.max_retries + 1) if step.on_error == "retry" else 1
        last_result: Any = None
        last_err: Optional[str] = None

        for attempt in range(max_attempts):
            self.trace.log({
                "event": "tool_call",
                "stage": "executor",
                "req_id": req_id,
                "step_id": step.step_id,
                "tool": step.tool,
                "args": norm_args,
                "attempt": attempt + 1,
            })

            raw = self.mcp.call(step.tool, args=norm_args)
            tool_err = _is_tool_error(raw)

            self.trace.log({
                "event": "tool_result",
                "stage": "executor",
                "req_id": req_id,
                "step_id": step.step_id,
                "tool": step.tool,
                "ok": tool_err is None,
                "error": tool_err,
                "result_summary": _summarize_tool_result(raw),
            })

            last_result = raw
            last_err = tool_err

            if tool_err is None:
                return StepResult(
                    step_id=step.step_id, tool=step.tool, args=norm_args,
                    ok=True, error=None, raw_result=raw,
                    retries_used=attempt, skipped=False,
                )

        # All attempts failed
        if step.on_error == "skip":
            return StepResult(
                step_id=step.step_id, tool=step.tool, args=norm_args,
                ok=False, error=last_err, raw_result=last_result,
                retries_used=max_attempts - 1, skipped=True,
            )

        return StepResult(
            step_id=step.step_id, tool=step.tool, args=norm_args,
            ok=False, error=last_err, raw_result=last_result,
            retries_used=max_attempts - 1, skipped=False,
        )
