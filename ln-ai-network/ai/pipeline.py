from __future__ import annotations

# Standard library
import json
import time
import traceback
from typing import Any, Dict, List, Optional

# Internal modules
from ai.command_queue import read_new, write_outbox
from ai.controllers.executor import Executor, ExecutorConfig, ExecutorError
from ai.controllers.planner import Planner, PlannerConfig, PlannerError
from ai.controllers.summarizer import Summarizer, SummarizerConfig
from ai.controllers.translator import Translator, TranslatorConfig, TranslatorError
from ai.core.config import AgentConfig
from ai.core.registry import AgentRegistry
from ai.core.scheduler import DeterministicScheduler
from ai.llm.factory import create_backend_for_role
from ai.llm.guarded_backend import GuardedBackend
from ai.mcp_client import FastMCPClientWrapper, MCPClient
from ai.models import IntentBlock, PipelineResult
from ai.utils import (
    StartupLock,
    TraceLogger,
    _env_bool,
    _env_int,
    _repo_root,
    _runtime_agent_dir,
)
from mcp.client.fastmcp import FastMCPClient

# Human-readable version string embedded in every pipeline report.
# Changing this string signals to the UI that the codebase was updated.
PIPELINE_BUILD = "pipeline-v1(translator+planner+executor+summarizer+history+verify)"

# Maps each state-changing intent type to a read-only MCP tool used to confirm
# the operation actually took effect after execution. Called in _verify_goal().
# Only intent types that have a natural read-back are included — noop and
# freeform are omitted because there's no canonical confirmation tool.
#
# Note: all listed tools require a "node" arg. _verify_goal() derives the node
# from intent.context["from_node"] or intent.context["node"], defaulting to 1.
_VERIFY_TOOL: Dict[str, str] = {
    "pay_invoice":  "ln_listfunds",    # ln_listfunds shows balance post-payment (ln_listpays doesn't exist)
    "open_channel": "ln_listchannels",
    "rebalance":    "ln_listchannels",
    "set_fee":      "ln_listchannels",
}


# =============================================================================
# Pipeline coordinator
# =============================================================================

class PipelineCoordinator:
    """
    Top-level orchestrator for the 4-stage AI pipeline:
      1. Translator  — NL prompt → structured IntentBlock (via LLM)
      2. Planner     — IntentBlock → ordered ExecutionPlan (via LLM)
      3. Executor    — ExecutionPlan → step-by-step MCP tool calls
      4. Summarizer  — tool results → human-readable answer (via LLM)

    One PipelineCoordinator runs as a long-lived process, polling the inbox
    file for new commands at a configurable tick rate. Results are written to
    the outbox file, where the UI server picks them up via SSE.

    Architecture note: each stage uses a *separate* LLM backend instance
    (created via create_backend_for_role). This allows different models or
    temperature settings per stage without sharing conversation state.
    """

    def __init__(self) -> None:
        repo_root = _repo_root()

        # Acquire the single-instance lock before doing any real work.
        # If another pipeline is running this will print an error and exit(1).
        lock_path = repo_root / "runtime" / "agent" / "pipeline.lock"
        self._lock = StartupLock(lock_path, name="pipeline")
        self._lock.acquire_or_exit()

        # Centralised configuration — all env vars read once at startup
        self._cfg = AgentConfig.from_env()

        # MCP client — wraps FastMCPClient so tool calls go through a uniform
        # MCPClient interface (call, list_tools). FastMCPClientWrapper adds
        # connection management and retry logic on top of the bare FastMCPClient.
        self.mcp: MCPClient = FastMCPClientWrapper(FastMCPClient())

        # Each stage gets its own LLM backend wrapped in GuardedBackend, which
        # enforces rate limiting, exponential backoff, and concurrency control
        # using the values from AgentConfig. Each stage has independent state
        # so a rate-limit on the summarizer does not block the translator.
        translator_backend = GuardedBackend(create_backend_for_role("translator"), self._cfg)
        planner_backend    = GuardedBackend(create_backend_for_role("planner"),     self._cfg)
        summarizer_backend = GuardedBackend(create_backend_for_role("summarizer"),  self._cfg)

        # Shared trace logger — all four stages write to the same trace.log file
        self.trace = TraceLogger(_runtime_agent_dir() / "trace.log")

        # Instantiate each stage controller, injecting shared dependencies
        self.translator = Translator(TranslatorConfig.from_env(), translator_backend, self.trace)
        self.planner = Planner(PlannerConfig.from_env(), planner_backend, self.trace)
        self.executor = Executor(ExecutorConfig.from_env(), self.mcp, self.trace)
        self.summarizer = Summarizer(SummarizerConfig.from_env(), summarizer_backend, self.trace)

        # Drift-free fixed-interval scheduler — fires every cfg.tick_ms milliseconds.
        # Uses DeterministicScheduler instead of naive sleep() so slow queries
        # don't cause cumulative drift in the inbox poll cadence.
        self._sched = DeterministicScheduler(self._cfg.tick_ms)

        # Master kill switch: if ALLOW_LLM != 1, every freeform query is
        # immediately rejected. Prevents accidental API usage during dev/testing.
        self.allow_llm = _env_bool("ALLOW_LLM", default=False)

        # Agent registry — register this pipeline so other processes can route
        # messages to it, and clean up stale entries from previous runs.
        self._registry = AgentRegistry(_repo_root() / "runtime" / "registry.jsonl")
        self._registry.purge_stale()
        self._node = _env_int("NODE_NUMBER", 1)
        self._registry.register(
            "pipeline", node=self._node,
            inbox_path=_runtime_agent_dir() / "inbox.jsonl",
        )

        # Persistent conversation history file — survives process restarts.
        # Each line is a JSON {"role": ..., "content": ...} message.
        self._history_path = _runtime_agent_dir() / "history.jsonl"

        # Rolling conversation history: list of {"role": "user"|"assistant", "content": str}
        # Injected into the Translator's messages so follow-up queries have context.
        # Loaded from disk on startup so context is preserved across restarts.
        self._history: List[Dict[str, Any]] = self._load_history()

    def _load_history(self) -> List[Dict[str, Any]]:
        """
        Load conversation history from disk on startup.

        Reads the last cfg.max_history_messages * 2 lines from history.jsonl
        so the history window is immediately populated after a restart. Lines
        that fail to parse are silently skipped (corrupted entries don't block startup).
        """
        if not self._history_path.exists():
            return []
        try:
            lines = self._history_path.read_text(encoding="utf-8").splitlines()
            history = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    history.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            max_msgs = self._cfg.max_history_messages * 2
            return history[-max_msgs:] if len(history) > max_msgs else history
        except Exception:
            return []

    def _log(self, kind: str, payload: Dict[str, Any]) -> None:
        """Emit a structured JSON log line to stdout (picked up by the process supervisor)."""
        out = {"ts": int(time.time()), "kind": kind, **payload}
        print(json.dumps(out, ensure_ascii=False), flush=True)

    def _write_report(self, result: PipelineResult) -> None:
        """Serialize and append the pipeline result to the outbox JSONL file."""
        write_outbox(result.to_outbox_dict())

    def _update_history(self, user_text: str, assistant_summary: str) -> None:
        """
        Append the latest exchange to the rolling history buffer and trim to
        cfg.max_history_messages pairs.

        We store the intent's goal string (not the full verbose summary) as the
        assistant turn. This keeps the history compact and avoids injecting raw
        tool output JSON into subsequent prompts.
        """
        new_msgs = [
            {"role": "user",      "content": user_text},
            {"role": "assistant", "content": assistant_summary},
        ]
        self._history.extend(new_msgs)
        # Keep only the last N*2 messages (N user+assistant pairs) in memory
        max_msgs = self._cfg.max_history_messages * 2
        if len(self._history) > max_msgs:
            self._history = self._history[-max_msgs:]
        # Append the new pair to disk so it survives restarts (best-effort)
        try:
            with self._history_path.open("a", encoding="utf-8") as fh:
                for msg in new_msgs:
                    fh.write(json.dumps(msg, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _verify_goal(self, intent: IntentBlock, req_id: int) -> Optional[str]:
        """
        Post-execution read-only verification for state-changing intents.

        After an executor run that reports success, we call a read-only tool
        (from _VERIFY_TOOL) to confirm the state actually changed. This catches
        cases where a tool reported ok=True but the change didn't persist
        (e.g., a race condition or a silent MCP-level rollback).

        Returns a short confirmation string appended to the human summary,
        or None if:
          - No verification tool is defined for this intent type
          - The MCP call fails (we don't want a verify failure to override
            a successful execution report — it's advisory only)
        """
        tool = _VERIFY_TOOL.get(intent.intent_type)
        if not tool:
            return None  # Intents like "noop" and "freeform" have no canonical verify
        # Derive the relevant node from intent context. Use from_node (the initiating
        # node for channel/payment operations) or node, defaulting to 1.
        node = intent.context.get("from_node") or intent.context.get("node") or 1
        try:
            raw = self.mcp.call(tool, {"node": node})
            self.trace.log({"event": "goal_verify", "req_id": req_id, "tool": tool, "ok": True})
            # Summarise: just confirm we got a non-error response with real data
            if isinstance(raw, dict):
                payload = raw.get("result", raw).get("payload", {})
                if isinstance(payload, dict):
                    keys = list(payload.keys())[:3]  # Show up to 3 keys as proof of data
                    return f"Verified via {tool}: {', '.join(keys) if keys else 'ok'}"
            return f"Verified via {tool}: ok"
        except Exception as exc:
            self.trace.log({"event": "goal_verify_failed", "req_id": req_id, "tool": tool, "error": str(exc)})
            return None

    # -------------------------------------------------------------------------
    # Pipeline execution
    # -------------------------------------------------------------------------

    def _run_pipeline(self, req_id: int, user_text: str) -> PipelineResult:
        """
        Execute the full 4-stage pipeline for a single user query.

        Returns a PipelineResult regardless of outcome — failures at any stage
        produce a result with success=False and stage_failed set to the name
        of the stage that failed. The caller (_run() loop) always writes the
        result to the outbox so the UI updates even on error.

        Stage short-circuits:
          - If allow_llm is False, return immediately (no API call).
          - If intent_type is "noop", skip planner+executor (no tools needed).
          - If plan has no steps, skip executor.
          - Summarizer only runs if all executor steps succeeded.

        The ts captured at the top is shared across the full result so the UI
        can correlate the outbox entry with the trace header.
        """
        ts = int(time.time())

        # Guard: LLM is disabled globally
        if not self.allow_llm:
            self.trace.log({"event": "llm_disabled", "req_id": req_id})
            return PipelineResult(
                request_id=req_id, ts=ts, success=False,
                stage_failed="translator", intent=None, plan=None,
                step_results=[], human_summary="LLM is disabled (ALLOW_LLM!=1).",
                error="ALLOW_LLM!=1", pipeline_build=PIPELINE_BUILD,
            )

        # Stage 1: Translate — NL prompt → structured IntentBlock
        # Passes rolling history so follow-up queries understand prior context.
        try:
            intent = self.translator.translate(user_text, req_id, history=list(self._history))
        except TranslatorError as e:
            self.trace.log({"event": "stage_failed", "stage": "translator", "error": str(e)})
            return PipelineResult(
                request_id=req_id, ts=ts, success=False,
                stage_failed="translator", intent=None, plan=None,
                step_results=[], human_summary=f"Failed to parse intent: {e}",
                error=str(e), pipeline_build=PIPELINE_BUILD,
            )

        # Short-circuit: noop intent needs no plan or execution
        if intent.intent_type == "noop":
            return PipelineResult(
                request_id=req_id, ts=ts, success=True,
                stage_failed=None, intent=intent, plan=None,
                step_results=[], human_summary=intent.human_summary,
                error=None, pipeline_build=PIPELINE_BUILD,
            )

        # Stage 2: Plan — IntentBlock → ordered ExecutionPlan of MCP tool calls
        try:
            plan = self.planner.plan(intent, req_id)
        except PlannerError as e:
            self.trace.log({"event": "stage_failed", "stage": "planner", "error": str(e)})
            return PipelineResult(
                request_id=req_id, ts=ts, success=False,
                stage_failed="planner", intent=intent, plan=None,
                step_results=[], human_summary=f"Failed to create execution plan: {e}",
                error=str(e), pipeline_build=PIPELINE_BUILD,
            )

        # Short-circuit: planner returned an empty steps list (unusual but valid)
        if not plan.steps:
            return PipelineResult(
                request_id=req_id, ts=ts, success=True,
                stage_failed=None, intent=intent, plan=plan,
                step_results=[], human_summary=intent.human_summary,
                error=None, pipeline_build=PIPELINE_BUILD,
            )

        # Stage 3: Execute — run each plan step against the MCP server sequentially
        # ExecutorError carries partial_results so we can report what succeeded
        # before the failure, even when raising.
        try:
            step_results = self.executor.execute(plan, req_id)
        except ExecutorError as e:
            self.trace.log({"event": "stage_failed", "stage": "executor", "error": str(e)})
            return PipelineResult(
                request_id=req_id, ts=ts, success=False,
                stage_failed="executor", intent=intent, plan=plan,
                step_results=e.partial_results,  # Show partial progress in the UI
                human_summary=f"Execution failed: {e}",
                error=str(e), pipeline_build=PIPELINE_BUILD,
            )

        # all_ok: every step either succeeded (ok=True) or was intentionally
        # skipped (on_error=skip). A skipped step doesn't count as failure.
        all_ok = all(r.ok or r.skipped for r in step_results)

        # Stage 4: Summarize — LLM produces a human-readable answer from tool results
        # Only runs when all steps passed; otherwise falls back to the intent's
        # pre-computed human_summary string.
        if all_ok and step_results:
            try:
                summary = self.summarizer.summarize(intent, step_results, req_id)
            except Exception as e:
                # Summarizer failure is non-fatal — always fall back to the Translator's
                # pre-computed human_summary. Log the exception so it's visible in the trace.
                self.trace.log({"event": "summarizer_error", "req_id": req_id, "error": str(e)})
                summary = intent.human_summary
        else:
            summary = intent.human_summary

        # Post-execution goal verification: read the network state to confirm the
        # intent actually took effect. Appends a short note to the summary if verified.
        if all_ok:
            note = self._verify_goal(intent, req_id)
            if note:
                summary += f"\n\n{note}"

        return PipelineResult(
            request_id=req_id, ts=ts, success=all_ok,
            # stage_failed is "executor" even when all_ok=False because the executor
            # ran but not all steps passed (different from raising ExecutorError).
            stage_failed=None if all_ok else "executor",
            intent=intent, plan=plan, step_results=step_results,
            human_summary=summary,
            error=None, pipeline_build=PIPELINE_BUILD,
        )

    # -------------------------------------------------------------------------
    # Main loop
    # -------------------------------------------------------------------------

    def run(self) -> None:
        """
        Blocking event loop. Polls the inbox JSONL file for new commands,
        dispatches them through the pipeline, and writes results to the outbox.

        Message routing by meta.kind:
          "freeform" + use_llm=True  → full 4-stage pipeline + archive
          "health_check"             → immediate success acknowledgement
          (anything else)            → unknown command error response

        Timing: tick_s controls the minimum interval between inbox polls.
        If a query takes longer than tick_s, the next poll starts immediately
        after it finishes (no sleep). This prevents queue backup.

        Error handling: unhandled exceptions inside the loop are logged and
        swallowed so the process stays alive. KeyboardInterrupt causes a clean
        shutdown. The outer try/except does NOT catch SystemExit (raised by
        StartupLock), so the process exits cleanly if the lock is stolen.
        """
        self._log("pipeline_start", {
            "msg": "Pipeline online. Waiting for inbox commands.",
            "build": PIPELINE_BUILD,
        })

        while True:
            try:
                for msg in read_new():
                    req_id = int(msg.get("id", 0))
                    meta = msg.get("meta") or {}
                    kind = meta.get("kind")

                    if kind == "freeform" and bool(meta.get("use_llm", False)):
                        # Capture start_ts here (before reset) so the archive
                        # filename matches the timestamp in the trace header.
                        start_ts = int(time.time())
                        self.trace.reset({
                            "ts": start_ts,
                            "event": "prompt_start",
                            "build": PIPELINE_BUILD,
                            "request_id": req_id,
                            "user_text": str(msg.get("content", "")),
                        })
                        user_text = str(msg.get("content", ""))
                        result = self._run_pipeline(req_id, user_text=user_text)
                        self._write_report(result)

                        # Determine archive status:
                        #   "ok"      → all steps succeeded
                        #   "partial" → some steps ok/skipped but result.success=False
                        #               (e.g. executor didn't hard-abort but had failures)
                        #   "failed"  → no steps succeeded or pipeline aborted early
                        if result.success:
                            arch_status = "ok"
                        elif result.step_results and any(r.ok or r.skipped for r in result.step_results):
                            arch_status = "partial"
                        else:
                            arch_status = "failed"
                        self.trace.archive(req_id, start_ts, arch_status)

                        # Update rolling history so the next prompt has context.
                        # Store the intent's goal (concise) not the verbose summary
                        # (which may contain raw JSON from tool results).
                        history_summary = result.intent.goal if result.intent else result.human_summary
                        self._update_history(user_text, history_summary)

                    elif kind == "route":
                        # Inter-agent routing: forward the payload to another
                        # registered pipeline or agent process.
                        # meta must include: target_kind, target_node, and
                        # the message to forward as meta.payload.
                        target_kind = meta.get("target_kind", "pipeline")
                        target_node = int(meta.get("target_node", 1))
                        payload = meta.get("payload") or {}
                        ok = self._registry.route_to(target_kind, target_node, payload)
                        write_outbox({
                            "ts": int(time.time()),
                            "type": "pipeline_report",
                            "request_id": req_id,
                            "success": ok,
                            "content": (
                                f"Routed to {target_kind}:{target_node}."
                                if ok else
                                f"No live {target_kind}:{target_node} found in registry."
                            ),
                            "pipeline_build": PIPELINE_BUILD,
                        })

                    elif kind == "list_peers":
                        # Discovery: return all currently-running registered processes.
                        peers = self._registry.list_peers()
                        write_outbox({
                            "ts": int(time.time()),
                            "type": "pipeline_report",
                            "request_id": req_id,
                            "success": True,
                            "content": f"{len(peers)} peer(s) registered.",
                            "peers": peers,
                            "pipeline_build": PIPELINE_BUILD,
                        })

                    elif kind == "health_check":
                        # Lightweight ping — no LLM involved, just confirm we're alive
                        write_outbox({
                            "ts": int(time.time()),
                            "type": "pipeline_report",
                            "request_id": req_id,
                            "success": True,
                            "content": "Pipeline is running.",
                            "pipeline_build": PIPELINE_BUILD,
                        })
                    else:
                        write_outbox({
                            "ts": int(time.time()),
                            "type": "pipeline_report",
                            "request_id": req_id,
                            "success": False,
                            "content": f"Unknown/unsupported command kind: {kind}",
                            "pipeline_build": PIPELINE_BUILD,
                        })

            except KeyboardInterrupt:
                self._log("pipeline_stop", {"msg": "Shutdown requested."})
                break
            except Exception:
                # Log the full traceback but keep running — transient errors
                # (network blips, MCP timeouts) shouldn't kill the pipeline.
                self._log("pipeline_error", {})
                traceback.print_exc()

            # Drift-free sleep: targets the next absolute tick time so slow
            # queries don't cause cumulative drift in the inbox poll cadence.
            self._sched.wait_next_tick()


if __name__ == "__main__":
    PipelineCoordinator().run()
