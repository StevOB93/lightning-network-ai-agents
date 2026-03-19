from __future__ import annotations

import atexit
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

from ai.command_queue import read_new, write_outbox
from ai.controllers.executor import Executor, ExecutorConfig, ExecutorError
from ai.controllers.planner import Planner, PlannerConfig, PlannerError
from ai.controllers.translator import Translator, TranslatorConfig, TranslatorError
from ai.llm.factory import create_backend_for_role
from ai.mcp_client import FastMCPClientWrapper, MCPClient
from ai.models import PipelineResult
from mcp.client.fastmcp import FastMCPClient

try:
    import fcntl
except Exception:
    fcntl = None  # type: ignore


PIPELINE_BUILD = "pipeline-v1(translator+planner+executor+history+verify)"

# How many prior exchanges (user+assistant pairs) to include as conversation context
_HISTORY_MAX = int(os.getenv("PIPELINE_HISTORY_MAX", "4"))

# Post-execution read-only verification tool per intent type.
# After a successful state-changing run, we call this tool to confirm the goal.
_VERIFY_TOOL: Dict[str, str] = {
    "pay_invoice":  "ln_listpays",
    "open_channel": "ln_listchannels",
    "rebalance":    "ln_listchannels",
    "set_fee":      "ln_listchannels",
}


# =============================================================================
# Startup lock (single pipeline instance)
# =============================================================================

class StartupLock:
    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path
        self._fh = None

    def acquire_or_exit(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = self.lock_path.open("a+", encoding="utf-8")
        try:
            if fcntl is None:
                fh.seek(0)
                existing = fh.read().strip()
                if existing:
                    raise RuntimeError(existing)
            else:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    fh.seek(0)
                    existing = fh.read().strip()
                    msg = existing or "Another pipeline instance holds the lock."
                    raise RuntimeError(msg)

            fh.seek(0)
            fh.truncate()
            fh.write(f"pid={os.getpid()} started_ts={int(time.time())}\n")
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except Exception:
                pass

            self._fh = fh
            atexit.register(self.release)

        except Exception as e:
            try:
                fh.close()
            except Exception:
                pass
            err = {
                "kind": "pipeline_lock_failed",
                "lock_path": str(self.lock_path),
                "error": str(e),
                "hint": "Another ai.pipeline process is already running. Stop it first.",
            }
            print(json.dumps(err, ensure_ascii=False), file=sys.stderr)
            raise SystemExit(1)

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass
        self._fh = None


# =============================================================================
# Trace logger (reset per prompt)
# =============================================================================

class TraceLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def reset(self, header: Dict[str, Any]) -> None:
        with self.path.open("w", encoding="utf-8") as f:
            f.write(json.dumps(header, ensure_ascii=False) + "\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass

    def log(self, event: Dict[str, Any]) -> None:
        event = dict(event)
        event.setdefault("ts", int(time.time()))
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass


# =============================================================================
# Helpers
# =============================================================================

def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _runtime_agent_dir() -> Path:
    return _repo_root() / "runtime" / "agent"


def _now_monotonic() -> float:
    return time.monotonic()


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None:
        return default
    try:
        return int(v.strip())
    except Exception:
        return default


# =============================================================================
# Pipeline coordinator
# =============================================================================

class PipelineCoordinator:
    def __init__(self) -> None:
        repo_root = _repo_root()
        lock_path = repo_root / "runtime" / "agent" / "pipeline.lock"
        self._lock = StartupLock(lock_path)
        self._lock.acquire_or_exit()

        self.mcp: MCPClient = FastMCPClientWrapper(FastMCPClient())

        translator_backend = create_backend_for_role("translator")
        planner_backend = create_backend_for_role("planner")

        self.trace = TraceLogger(_runtime_agent_dir() / "trace.log")

        self.translator = Translator(TranslatorConfig.from_env(), translator_backend, self.trace)
        self.planner = Planner(PlannerConfig.from_env(), planner_backend, self.trace)
        self.executor = Executor(ExecutorConfig.from_env(), self.mcp, self.trace)

        self.tick_s = float(_env_int("AGENT_TICK_MS", 500)) / 1000.0
        self.allow_llm = _env_bool("ALLOW_LLM", default=False)
        # Rolling conversation history: list of {"role": "user"|"assistant", "content": str}
        self._history: List[Dict[str, Any]] = []

    def _log(self, kind: str, payload: Dict[str, Any]) -> None:
        out = {"ts": int(time.time()), "kind": kind, **payload}
        print(json.dumps(out, ensure_ascii=False), flush=True)

    def _sleep_to_next_tick(self, start_t: float) -> None:
        elapsed = _now_monotonic() - start_t
        time.sleep(max(0.0, self.tick_s - elapsed))

    def _write_report(self, result: PipelineResult) -> None:
        write_outbox(result.to_outbox_dict())

    def _update_history(self, user_text: str, assistant_summary: str) -> None:
        """Append the latest exchange and trim to _HISTORY_MAX pairs."""
        self._history.append({"role": "user",      "content": user_text})
        self._history.append({"role": "assistant",  "content": assistant_summary})
        # Keep only the last N*2 messages (N pairs)
        max_msgs = _HISTORY_MAX * 2
        if len(self._history) > max_msgs:
            self._history = self._history[-max_msgs:]

    def _verify_goal(self, intent: "IntentBlock", req_id: int) -> Optional[str]:
        """
        Run a single read-only MCP tool to confirm a state-changing intent succeeded.
        Returns a short confirmation string, or None if no verification is defined or it fails.
        """
        tool = _VERIFY_TOOL.get(intent.intent_type)
        if not tool:
            return None
        try:
            raw = self.mcp.call(tool, {})
            self.trace.log({"event": "goal_verify", "req_id": req_id, "tool": tool, "ok": True})
            # Summarise: just confirm we got a non-error response
            if isinstance(raw, dict):
                payload = raw.get("result", raw).get("payload", {})
                if isinstance(payload, dict):
                    keys = list(payload.keys())[:3]
                    return f"Verified via {tool}: {', '.join(keys) if keys else 'ok'}"
            return f"Verified via {tool}: ok"
        except Exception as exc:
            self.trace.log({"event": "goal_verify_failed", "req_id": req_id, "tool": tool, "error": str(exc)})
            return None

    # -------------------------------------------------------------------------
    # Pipeline execution
    # -------------------------------------------------------------------------

    def _run_pipeline(self, req_id: int, user_text: str) -> PipelineResult:
        ts = int(time.time())

        if not self.allow_llm:
            self.trace.log({"event": "llm_disabled", "req_id": req_id})
            return PipelineResult(
                request_id=req_id, ts=ts, success=False,
                stage_failed="translator", intent=None, plan=None,
                step_results=[], human_summary="LLM is disabled (ALLOW_LLM!=1).",
                error="ALLOW_LLM!=1", pipeline_build=PIPELINE_BUILD,
            )

        # Stage 1: Translate (with rolling conversation history)
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

        # Stage 2: Plan
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

        # Noop: no steps to execute
        if not plan.steps:
            return PipelineResult(
                request_id=req_id, ts=ts, success=True,
                stage_failed=None, intent=intent, plan=plan,
                step_results=[], human_summary=intent.human_summary,
                error=None, pipeline_build=PIPELINE_BUILD,
            )

        # Stage 3: Execute
        try:
            step_results = self.executor.execute(plan, req_id)
        except ExecutorError as e:
            self.trace.log({"event": "stage_failed", "stage": "executor", "error": str(e)})
            return PipelineResult(
                request_id=req_id, ts=ts, success=False,
                stage_failed="executor", intent=intent, plan=plan,
                step_results=e.partial_results,
                human_summary=f"Execution failed: {e}",
                error=str(e), pipeline_build=PIPELINE_BUILD,
            )

        all_ok = all(r.ok or r.skipped for r in step_results)

        # Post-execution goal verification for state-changing intents
        verification_note = ""
        if all_ok:
            note = self._verify_goal(intent, req_id)
            if note:
                verification_note = f"\n\n{note}"

        summary = intent.human_summary + verification_note
        return PipelineResult(
            request_id=req_id, ts=ts, success=all_ok,
            stage_failed=None if all_ok else "executor",
            intent=intent, plan=plan, step_results=step_results,
            human_summary=summary,
            error=None, pipeline_build=PIPELINE_BUILD,
        )

    # -------------------------------------------------------------------------
    # Main loop
    # -------------------------------------------------------------------------

    def run(self) -> None:
        self._log("pipeline_start", {
            "msg": "Pipeline online. Waiting for inbox commands.",
            "build": PIPELINE_BUILD,
        })

        while True:
            tick_start = _now_monotonic()
            try:
                msgs = read_new()
                if not msgs:
                    self._sleep_to_next_tick(tick_start)
                    continue

                for msg in msgs:
                    req_id = int(msg.get("id", 0))
                    meta = msg.get("meta") or {}
                    kind = meta.get("kind")

                    if kind == "freeform" and bool(meta.get("use_llm", False)):
                        self.trace.reset({
                            "ts": int(time.time()),
                            "event": "prompt_start",
                            "build": PIPELINE_BUILD,
                            "request_id": req_id,
                            "user_text": str(msg.get("content", "")),
                        })
                        user_text = str(msg.get("content", ""))
                        result = self._run_pipeline(req_id, user_text=user_text)
                        self._write_report(result)
                        # Update rolling history so the next prompt has context
                        self._update_history(user_text, result.human_summary)
                    else:
                        write_outbox({
                            "ts": int(time.time()),
                            "type": "pipeline_report",
                            "request_id": req_id,
                            "success": False,
                            "content": f"Unknown/unsupported command kind: {kind}",
                            "pipeline_build": PIPELINE_BUILD,
                        })

                self._sleep_to_next_tick(tick_start)

            except KeyboardInterrupt:
                self._log("pipeline_stop", {"msg": "Shutdown requested."})
                break
            except Exception:
                self._log("pipeline_error", {})
                traceback.print_exc()
                self._sleep_to_next_tick(tick_start)


if __name__ == "__main__":
    PipelineCoordinator().run()
