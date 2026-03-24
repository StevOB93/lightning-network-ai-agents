from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ai.controllers.shared import _env_int
from ai.mcp_client import MCPClient, MCPTimeoutError
from ai.models import ExecutionPlan, PlanStep, StepResult
from ai.tools import _is_tool_error, _normalize_tool_args, _summarize_tool_result


# =============================================================================
# Error
# =============================================================================

class ExecutorError(Exception):
    """
    Raised when a non-skippable step fails during execution.

    Carries partial_results so the pipeline coordinator can report the
    steps that succeeded before the failure. This lets the UI show a
    partial execution trace even when the overall plan failed.
    """
    def __init__(self, message: str, partial_results: Optional[List[StepResult]] = None) -> None:
        super().__init__(message)
        self.partial_results: List[StepResult] = partial_results or []


# =============================================================================
# Config
# =============================================================================

@dataclass(frozen=True)
class ExecutorConfig:
    """
    Immutable configuration for the Executor.

    default_on_error: The fallback error policy for steps that don't specify
      one explicitly. "abort" is the safe default — stop execution on any failure.
      Set to "skip" to allow a plan to continue past failures (useful for
      diagnostic plans where some tools may not be available).

    max_workers: Maximum number of steps to run concurrently within a single
      dependency wave. Default is 1 (strictly sequential, always safe).
      Set to > 1 to enable parallel execution for independent plan steps.

      WARNING: Setting max_workers > 1 requires a thread-safe MCP client.
      FastMCPClientWrapper (the production client) uses a single connection and
      is NOT thread-safe — concurrent calls may interleave or corrupt each other.
      Only increase this value if you have verified your MCP client is thread-safe
      or are using a client that creates separate connections per call.
    """
    default_on_error: str = "abort"
    max_workers: int = 1

    @staticmethod
    def from_env() -> ExecutorConfig:
        return ExecutorConfig(
            default_on_error=os.getenv("EXECUTOR_DEFAULT_ON_ERROR", "abort"),
            max_workers=_env_int("EXECUTOR_MAX_WORKERS", 1),
        )


# =============================================================================
# Dependency ordering
# =============================================================================

def _topological_sort(steps: List[PlanStep]) -> List[PlanStep]:
    """
    Sort plan steps into a valid execution order that respects depends_on.

    Uses depth-first DFS. Raises ValueError on:
      - Circular dependencies (a → b → a)
      - References to unknown step_ids in depends_on

    For plans with no dependency constraints (all depends_on=[]) this is
    equivalent to the original order. The sort is otherwise stable: steps
    that share a topological level appear in ascending step_id order.

    This is a prerequisite for parallel execution: once the topological
    order is known, steps at the same level can run concurrently.
    """
    by_id: Dict[int, PlanStep] = {s.step_id: s for s in steps}
    order: List[PlanStep] = []
    visited: set = set()
    in_progress: set = set()  # cycle detection

    def _visit(step_id: int) -> None:
        if step_id in visited:
            return
        if step_id in in_progress:
            raise ValueError(f"Circular dependency detected involving step {step_id}")
        if step_id not in by_id:
            raise ValueError(f"Unknown step_id {step_id} referenced in depends_on")
        in_progress.add(step_id)
        for dep_id in sorted(by_id[step_id].depends_on):  # sorted for stable ordering
            _visit(dep_id)
        in_progress.discard(step_id)
        visited.add(step_id)
        order.append(by_id[step_id])

    for step in sorted(steps, key=lambda s: s.step_id):  # deterministic input order
        _visit(step.step_id)

    return order


def _compute_levels(sorted_steps: List[PlanStep]) -> List[List[PlanStep]]:
    """
    Group topologically-sorted steps into parallel execution waves.

    Steps in the same wave have no dependencies on each other, so they can
    run concurrently. Steps in wave N+1 all depend on at least one step in
    wave N (or earlier).

    Input must be the output of _topological_sort() (valid topological order).
    This function does not validate the graph — call _topological_sort() first.

    Example:
      step1 (no deps), step2 (no deps), step3 (deps=[1]), step4 (deps=[2,3])
      → wave 0: [step1, step2]
        wave 1: [step3]       (step1 is done after wave 0)
        wave 2: [step4]       (both step2 and step3 are done)
    """
    completed: set = set()
    remaining = list(sorted_steps)
    levels: List[List[PlanStep]] = []

    while remaining:
        wave = [s for s in remaining if all(d in completed for d in s.depends_on)]
        if not wave:
            # Should be unreachable if input is a valid topological order
            raise ValueError("_compute_levels: no progress possible — check dependency graph")
        levels.append(wave)
        for s in wave:
            completed.add(s.step_id)
        remaining = [s for s in remaining if s.step_id not in completed]

    return levels


# =============================================================================
# Placeholder resolution
# =============================================================================

# Matches "$stepN.dot.separated.path" where N is any positive integer.
# Example: "$step1.result.payload.bolt11"
_PLACEHOLDER_RE = re.compile(r"^\$step(\d+)\.(.+)$")

# Matches "$context.field_name" — references the intent's context dict.
# Example: "$context.from_node"
_CONTEXT_RE = re.compile(r"^\$context\.(.+)$")


def _navigate(obj: Any, path: str) -> Any:
    """
    Navigate a dot-separated path into a nested dict/list structure.

    Supports both dict key access and list index access (integer parts).
    Accepts both dot notation (.0) and bracket notation ([0]) for list indices —
    bracket notation is normalised to dot notation before processing so that
    LLM-generated placeholder paths like "payload.binding[0].port" work the
    same as "payload.binding.0.port".

    Raises KeyError with a descriptive message on any navigation failure
    so the caller can surface it as a placeholder resolution error.

    Examples:
      _navigate({"result": {"payload": {"bolt11": "lnbc..."}}}, "result.payload.bolt11")
      → "lnbc..."
      _navigate({"payload": {"binding": [{"port": 9735}]}}, "payload.binding[0].port")
      → 9735
    """
    # Normalise bracket notation: "binding[0].port" → "binding.0.port"
    path = re.sub(r"\[(\d+)\]", r".\1", path)
    cur = obj
    for part in path.split("."):
        if not part:
            continue  # skip empty parts from leading/trailing dots
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


def _resolve_value(val: Any, results_by_id: Dict[int, StepResult], context: Optional[Dict[str, Any]] = None) -> Any:
    """
    Resolve a single argument value, substituting any placeholder references.

    Placeholder types:
      $stepN.path  → navigate into step N's raw_result using dot path
      $context.key → look up key in the intent's context dict

    Non-string values (ints, bools, etc.) are returned unchanged.
    String values that don't match a placeholder pattern are returned as-is.

    Raises KeyError with a descriptive message if:
      - The referenced step hasn't been executed yet
      - The path doesn't exist in the step's raw result
      - The context key doesn't exist
    """
    if not isinstance(val, str):
        return val  # Only strings can be placeholders

    # Check for $context.field first (more specific pattern)
    mc = _CONTEXT_RE.match(val)
    if mc:
        if context is None:
            raise KeyError(f"placeholder '{val}' requires intent context but context is None")
        field = mc.group(1)
        if field in context:
            return context[field]
        raise KeyError(f"placeholder '{val}' references context field '{field}' which is not present")

    # Check for $stepN.path placeholder
    m = _PLACEHOLDER_RE.match(val)
    if not m:
        return val  # Plain string, no substitution needed

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


def _resolve_args(args: Dict[str, Any], results_by_id: Dict[int, StepResult], context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Resolve all placeholder values in a step's args dict.

    Returns a new dict with all $stepN.path and $context.field strings
    replaced by their resolved values. Non-placeholder values pass through
    unchanged.
    """
    return {k: _resolve_value(v, results_by_id, context) for k, v in args.items()}


# =============================================================================
# Executor
# =============================================================================

class Executor:
    """
    Stage 3 of the pipeline: executes an ordered ExecutionPlan against the
    MCP server, resolving inter-step placeholders at runtime.

    Execution respects depends_on: steps are grouped into parallel waves by
    _compute_levels(), and steps within the same wave run concurrently when
    max_workers > 1. Each step can reference outputs from completed steps in
    prior waves via $stepN.path placeholders.

    Error handling per step is controlled by the step's on_error field:
      abort  → raise ExecutorError immediately (with partial results so far)
      retry  → re-attempt up to max_retries times before aborting
      skip   → return a skipped StepResult and continue to the next step

    The Executor does not do any LLM calls — it is a pure execution engine
    that translates a plan into MCP tool calls.
    """

    def __init__(
        self,
        config: ExecutorConfig,
        mcp: MCPClient,
        trace: Any,
    ) -> None:
        self.config = config
        self.mcp = mcp
        self.trace = trace  # TraceLogger shared with all pipeline stages

        # Warn when parallel execution is enabled with a non-thread-safe MCP client.
        # FastMCPClientWrapper serialises calls through a single threading.Lock, so
        # the calls will be safe but sequential — parallel waves won't actually run
        # concurrently. More importantly, if a different (non-locking) MCP client is
        # ever substituted, the race condition would be silent and hard to diagnose.
        # This warning makes the configuration choice visible in the logs immediately.
        if self.config.max_workers > 1:
            print(json.dumps({
                "ts": int(time.time()),
                "kind": "executor_config_warning",
                "msg": (
                    f"EXECUTOR_MAX_WORKERS={self.config.max_workers} enables parallel "
                    "step execution. FastMCPClientWrapper (the default MCP client) "
                    "serialises all calls through a Lock, so steps will run "
                    "sequentially at the MCP boundary regardless. "
                    "Only use max_workers > 1 with a connection-pooled MCP client "
                    "that is explicitly documented as thread-safe. "
                    "Set EXECUTOR_MAX_WORKERS=1 to suppress this warning."
                ),
            }), flush=True)

    def execute(self, plan: ExecutionPlan, req_id: int) -> List[StepResult]:
        """
        Execute plan steps in topological dependency order, running independent
        steps within each wave in parallel when max_workers > 1.

        Execution model:
          1. _topological_sort()  — validate deps and produce a valid order
          2. _compute_levels()    — group into parallel waves (steps with no
                                    inter-dependencies within a wave)
          3. For each wave:
               max_workers == 1 → sequential (safe with any MCP client)
               max_workers > 1  → ThreadPoolExecutor for concurrent MCP calls
                                   (requires a thread-safe MCP client)

        Thread safety: results_by_id is read-only inside each wave (threads
        can only see results from prior waves, which are fully written before
        the wave starts). Writes happen on the main thread between waves.

        Returns list of StepResult ordered by step_id.
        Raises ExecutorError if a non-skippable step fails, or if the plan
        has an invalid dependency graph (circular or unknown step references).
        """
        # Validate and sort steps before starting any tool calls.
        try:
            ordered_steps = _topological_sort(plan.steps)
        except ValueError as e:
            raise ExecutorError(f"Invalid plan dependency graph: {e}")

        waves = _compute_levels(ordered_steps)
        results: List[StepResult] = []
        results_by_id: Dict[int, StepResult] = {}
        context = plan.intent.context if plan.intent else {}

        for wave in waves:
            wave_results = self._execute_wave(wave, req_id, results_by_id, context)

            # Commit wave results before the next wave reads them via placeholders
            for r in wave_results:
                results_by_id[r.step_id] = r
            results.extend(wave_results)

            # Check for hard failures after the full wave completes.
            # We let the entire wave finish before raising so the trace log
            # captures all results (including sibling failures) for the UI.
            failed = [r for r in wave_results if not r.ok and not r.skipped]
            if failed:
                raise ExecutorError(
                    f"Step {failed[0].step_id} ({failed[0].tool}) failed: {failed[0].error}",
                    partial_results=results,
                )

        return results

    def _execute_wave(
        self,
        wave: List[PlanStep],
        req_id: int,
        results_by_id: Dict[int, StepResult],
        context: Optional[Dict[str, Any]],
    ) -> List[StepResult]:
        """
        Execute all steps in a single parallel wave.

        Sequential mode (max_workers=1): steps run one at a time; safe with
          any MCP client.
        Parallel mode (max_workers>1): steps run concurrently via
          ThreadPoolExecutor. results_by_id is passed read-only — steps in the
          same wave cannot reference each other's outputs via placeholders.

        Exceptions raised by _execute_step (ExecutorError for placeholder or
        args failures) are caught here and converted to failed StepResults so
        that sibling steps in the wave can still finish. The caller checks the
        results for failures after the wave completes.
        """
        if self.config.max_workers <= 1 or len(wave) == 1:
            # Sequential fast path — no thread overhead for single-step waves
            # or when parallelism is disabled.
            #
            # Sequential mode: results_by_id is updated after each step so that
            # later steps in the same wave can reference earlier results via
            # $stepN.path placeholders even without explicit depends_on declarations.
            # Parallel mode (max_workers>1): results_by_id is read-only within a wave;
            # steps in the same wave cannot reference each other's outputs — use
            # depends_on to force ordering if cross-step references are needed.
            # On an abort failure, execution stops early (same as pre-wave behavior).
            wave_results: List[StepResult] = []
            for step in wave:
                try:
                    result = self._execute_step(step, req_id, results_by_id, context)
                except ExecutorError as e:
                    result = StepResult(
                        step_id=step.step_id, tool=step.tool, args=step.args,
                        ok=False, error=str(e), raw_result=None,
                        retries_used=0, skipped=False,
                    )
                wave_results.append(result)
                results_by_id[step.step_id] = result  # visible to subsequent steps in this wave
                if not result.ok and not result.skipped:
                    break  # abort: stop running later steps in this wave
            return wave_results

        # Parallel path: submit all wave steps to a thread pool.
        # results_by_id is read-only in this scope (written between waves on
        # the main thread) so no locking is required.
        workers = min(len(wave), self.config.max_workers)
        wave_results = [None] * len(wave)  # pre-allocate to preserve order
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_to_idx = {
                pool.submit(self._execute_step, step, req_id, results_by_id, context): i
                for i, step in enumerate(wave)
            }
            for future in as_completed(future_to_idx):
                idx = future_to_idx[future]
                step = wave[idx]
                try:
                    wave_results[idx] = future.result()
                except ExecutorError as e:
                    wave_results[idx] = StepResult(
                        step_id=step.step_id, tool=step.tool, args=step.args,
                        ok=False, error=str(e), raw_result=None,
                        retries_used=0, skipped=False,
                    )
        return wave_results  # type: ignore[return-value]  # all slots filled

    def _execute_step(
        self,
        step: PlanStep,
        req_id: int,
        results_by_id: Dict[int, StepResult],
        context: Optional[Dict[str, Any]] = None,
    ) -> StepResult:
        """
        Execute a single plan step:
          1. Log step_start
          2. Resolve $stepN.path and $context.field placeholders in args
          3. Normalize and validate resolved args (type coercion, required key check)
          4. Call the MCP tool, retrying if on_error="retry"
          5. Return StepResult with ok/skipped/error status

        Placeholder resolution errors and arg validation errors are treated
        like tool errors — they respect the step's on_error policy.
        """
        self.trace.log({
            "event": "step_start",
            "stage": "executor",
            "req_id": req_id,
            "step_id": step.step_id,
            "tool": step.tool,
        })

        # Phase 1: Resolve placeholders
        # This may raise KeyError if a referenced step hasn't run yet
        # (which would be a plan ordering bug) or if the path doesn't exist
        # in the prior step's raw result.
        try:
            resolved_args = _resolve_args(step.args, results_by_id, context)
        except KeyError as e:
            err = f"Placeholder resolution failed: {e}"
            self.trace.log({
                "event": "step_placeholder_error",
                "stage": "executor",
                "req_id": req_id,
                "step_id": step.step_id,
                "error": err,
            })
            # Respect on_error policy even for placeholder failures
            if step.on_error == "skip":
                return StepResult(
                    step_id=step.step_id, tool=step.tool, args=step.args,
                    ok=False, error=err, raw_result=None, retries_used=0, skipped=True,
                )
            raise ExecutorError(err)

        # Phase 2: Normalize and validate args
        # _normalize_tool_args handles:
        #   - Unwrapping nested {"args": {...}} shapes (LLM sometimes wraps)
        #   - Coercing "1" → 1 for known integer fields (node, amount_sat, etc.)
        #   - Checking that all required keys are present
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

        # Phase 3: Execute with retry
        # max_attempts is 1 for abort/skip policies, or (max_retries+1) for retry.
        # This avoids retry overhead for the common case.
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
                "attempt": attempt + 1,  # 1-based for human readability in the trace
            })

            try:
                raw = self.mcp.call(step.tool, args=norm_args)
            except MCPTimeoutError as _mcp_timeout:
                # The MCP server did not respond within the configured deadline.
                # Convert to an error dict so the existing on_error policy
                # (abort/skip/retry) governs what happens next — a timeout is
                # treated identically to a tool returning an error response.
                # Logging here makes the timeout visible in the UI trace panel.
                self.trace.log({
                    "event": "tool_timeout",
                    "stage": "executor",
                    "req_id": req_id,
                    "step_id": step.step_id,
                    "tool": step.tool,
                    "error": str(_mcp_timeout),
                })
                raw = {"error": str(_mcp_timeout)}
            except Exception as _mcp_exc:
                # Unexpected exception from the MCP client (e.g. subprocess crash,
                # JSON decode error). Convert to an error dict so the on_error
                # policy governs recovery rather than crashing the pipeline.
                _exc_msg = f"MCP client error: {_mcp_exc.__class__.__name__}: {_mcp_exc}"
                self.trace.log({
                    "event": "tool_error",
                    "stage": "executor",
                    "req_id": req_id,
                    "step_id": step.step_id,
                    "tool": step.tool,
                    "error": _exc_msg,
                })
                raw = {"error": _exc_msg}
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
                # Note: raw_result is NOT logged here to keep trace compact.
                # The full raw_result is stored in StepResult for the summarizer.
            })

            last_result = raw
            last_err = tool_err

            if tool_err is None:
                # Success — return immediately without waiting for more attempts
                return StepResult(
                    step_id=step.step_id, tool=step.tool, args=norm_args,
                    ok=True, error=None, raw_result=raw,
                    retries_used=attempt,  # 0 on first success, >0 if retried
                    skipped=False,
                )
            # If tool_err is not None and we have attempts remaining, loop continues

        # All attempts exhausted — return based on on_error policy
        if step.on_error == "skip":
            # Mark as skipped so the overall pipeline can continue
            return StepResult(
                step_id=step.step_id, tool=step.tool, args=norm_args,
                ok=False, error=last_err, raw_result=last_result,
                retries_used=max_attempts - 1, skipped=True,
            )

        # on_error == "abort" (or "retry" with all retries exhausted):
        # Return a non-skipped failure. The execute() loop will raise ExecutorError
        # when it sees ok=False and skipped=False.
        return StepResult(
            step_id=step.step_id, tool=step.tool, args=norm_args,
            ok=False, error=last_err, raw_result=last_result,
            retries_used=max_attempts - 1, skipped=False,
        )
