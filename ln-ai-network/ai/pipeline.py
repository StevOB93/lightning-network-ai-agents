from __future__ import annotations

# Standard library
import json
import os
import signal
import time
import traceback
from typing import Any, Dict, List, Optional

# fcntl provides advisory file locking on Unix/macOS. It is not available on
# Windows, so we import it with a None fallback and check before each use.
# Without locking, concurrent writes to history.jsonl (e.g. during an
# overlapping restart) could produce partial JSON lines that break history
# loading on the next startup.
try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None  # type: ignore[assignment]  # Windows — skip locking gracefully

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

        # Tier-3 episodic archive — append-only record of every completed run.
        # Never trimmed automatically; only wiped on `restart_agent.sh fresh`.
        # NOT injected into LLM context — only queried on demand via memory_lookup.
        self._archive_path = _runtime_agent_dir() / "archive.jsonl"

        # Rolling conversation history: list of {"role": "user"|"assistant", "content": str}
        # Injected into the Translator's messages so follow-up queries have context.
        # Loaded from disk on startup so context is preserved across restarts.
        self._history: List[Dict[str, Any]] = self._load_history()

        # Hot-reload flag: set by SIGHUP handler, checked in run loop
        self._reload_pending = False
        # Install SIGHUP handler (SIGTERM/SIGINT already terminate cleanly via KeyboardInterrupt)
        try:
            signal.signal(signal.SIGHUP, self._handle_sighup)
        except (OSError, ValueError):
            pass  # Not on UNIX or called from a thread — skip silently

    def _load_history(self) -> List[Dict[str, Any]]:
        """
        Load conversation history from disk on startup, validate each message,
        and compact the file if it exceeds the active rolling window.

        Validation: each message must have role="user"|"assistant" and a non-empty
        string content field. Invalid entries are silently dropped. Without this
        check, a manually edited or partially corrupted history.jsonl could inject
        malformed messages into the LLM API request, causing a hard API error.

        Compaction: if history.jsonl holds more lines than max_history_messages*2,
        the file is rewritten with only the most recent messages. This prevents
        unbounded file growth during long sessions — without compaction, every
        query adds two lines to the file forever. Compaction is a non-fatal
        best-effort operation; if the rewrite fails (e.g. disk full), the in-memory
        history is still correct, only the on-disk file stays larger than needed.

        The active rolling window (max_history_messages pairs) is what the
        Translator actually uses; older messages are never sent to the LLM, so
        there's no benefit to keeping them on disk.
        """
        if not self._history_path.exists():
            return []
        try:
            lines = self._history_path.read_text(encoding="utf-8").splitlines()

            # The set of role values the Translator accepts. Any other value would
            # be forwarded to the LLM API as-is, which most providers reject.
            _VALID_ROLES = {"user", "assistant"}

            all_parsed: List[Dict[str, Any]] = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue  # Skip malformed lines (e.g. from an interrupted write)

                # Validate the message shape before accepting it into the history.
                # We require:
                #   role    — must be "user" or "assistant" (what the LLM API accepts)
                #   content — must be a non-empty string (blank turns add noise)
                # Any message that fails this check is silently dropped; we don't
                # log here because this runs on every startup and would spam logs.
                if (
                    isinstance(obj, dict)
                    and obj.get("role") in _VALID_ROLES
                    and isinstance(obj.get("content"), str)
                    and obj["content"].strip()
                ):
                    all_parsed.append(obj)

            max_msgs = self._cfg.max_history_messages * 2
            history = all_parsed[-max_msgs:] if len(all_parsed) > max_msgs else all_parsed

            # Compact history.jsonl if we loaded more messages than the active window.
            # Rewrite the file with just the trimmed slice so it doesn't grow unbounded.
            # We lock the file during the write to prevent corruption if (unlikely)
            # another process is simultaneously appending.
            if len(all_parsed) > len(history):
                try:
                    # Atomic compaction: write to a temp file, fsync, then rename.
                    # This avoids truncating the original before the new data is
                    # safely on disk — a crash mid-write won't lose history.
                    import tempfile
                    tmp_fd, tmp_path = tempfile.mkstemp(
                        dir=str(self._history_path.parent),
                        prefix=".history_compact_",
                        suffix=".tmp",
                    )
                    try:
                        with os.fdopen(tmp_fd, "w", encoding="utf-8") as tmp_fh:
                            for msg in history:
                                tmp_fh.write(json.dumps(msg, ensure_ascii=False) + "\n")
                            tmp_fh.flush()
                            os.fsync(tmp_fh.fileno())
                        os.replace(tmp_path, str(self._history_path))
                    except Exception:
                        # Clean up the temp file on failure
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass
                        raise
                except Exception:
                    pass  # Compaction failure is non-fatal — history is correct in memory

            return history
        except Exception:
            return []

    def _log(self, kind: str, payload: Dict[str, Any]) -> None:
        """Emit a structured JSON log line to stdout (picked up by the process supervisor)."""
        out = {"ts": int(time.time()), "kind": kind, **payload}
        print(json.dumps(out, ensure_ascii=False), flush=True)

    def _write_report(self, result: PipelineResult) -> None:
        """Serialize and append the pipeline result to the outbox JSONL file."""
        write_outbox(result.to_outbox_dict())

    def _update_history(
        self,
        user_text: str,
        assistant_summary: str,
        outcome: str = "ok",
        human_summary: str = "",
    ) -> None:
        """
        Append the latest exchange to the rolling history buffer and trim to
        cfg.max_history_messages pairs.

        We store the intent's goal string (not the full verbose summary) as the
        assistant turn. This keeps the history compact and avoids injecting raw
        tool output JSON into subsequent prompts.

        Also appends one record to the tier-3 episodic archive (archive.jsonl)
        which is never trimmed and only queried on demand via memory_lookup.
        """
        # Deduplicate: if the last exchange in history is identical (same user text
        # AND same assistant goal), skip — repeated identical prompts (e.g. 10x demo
        # runs of the same command) would otherwise dominate the context window and
        # cause the translator to drift toward that old intent on unrelated prompts.
        if (len(self._history) >= 2
                and self._history[-2].get("content") == user_text
                and self._history[-1].get("content") == assistant_summary):
            return  # identical to last entry — no new information

        new_msgs = [
            {"role": "user",      "content": user_text},
            {"role": "assistant", "content": assistant_summary},
        ]
        self._history.extend(new_msgs)
        # Keep only the last N*2 messages (N user+assistant pairs) in memory
        max_msgs = self._cfg.max_history_messages * 2
        if len(self._history) > max_msgs:
            self._history = self._history[-max_msgs:]
        # Append the new pair to disk so it survives restarts (best-effort).
        # We lock the file before writing to prevent partial-line corruption if
        # two processes overlap (e.g. a fast restart while a write is in progress).
        # Without the lock, a crash mid-write leaves a truncated JSON line that
        # _load_history() would silently drop on next startup, losing context.
        try:
            with self._history_path.open("a", encoding="utf-8") as fh:
                if _fcntl is not None:
                    _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX)
                try:
                    for msg in new_msgs:
                        fh.write(json.dumps(msg, ensure_ascii=False) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())  # Ensure bytes reach disk before unlocking
                finally:
                    if _fcntl is not None:
                        _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)
        except Exception:
            pass  # History is advisory — a write failure never crashes the pipeline

        # Tier-3: append one record to the episodic archive (best-effort).
        # This file is never trimmed — it grows as a permanent audit log.
        try:
            archive_record = json.dumps({
                "ts": int(time.time()),
                "user": user_text,
                "goal": assistant_summary,
                "outcome": outcome,
                "summary": human_summary,
            }, ensure_ascii=False)
            with self._archive_path.open("a", encoding="utf-8") as fh:
                if _fcntl is not None:
                    _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX)
                try:
                    fh.write(archive_record + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                finally:
                    if _fcntl is not None:
                        _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)
        except Exception:
            pass  # Archive write failure is non-fatal

    def _handle_sighup(self, signum: int, frame: Any) -> None:
        """Mark config reload as pending (processed safely in the run loop)."""
        self._reload_pending = True

    def _reload_config(self) -> None:
        """
        Rebuild GuardedBackend instances and refresh AgentConfig from env vars.

        Called from the run loop when _reload_pending is True. Constructs a
        new AgentConfig snapshot from the current environment and replaces
        the three stage backends. The pipeline keeps running — in-flight
        queries use the old backends; new queries use the new ones.
        """
        self._reload_pending = False
        try:
            new_cfg = AgentConfig.from_env()
            translator_backend = GuardedBackend(create_backend_for_role("translator"), new_cfg)
            planner_backend    = GuardedBackend(create_backend_for_role("planner"),     new_cfg)
            summarizer_backend = GuardedBackend(create_backend_for_role("summarizer"),  new_cfg)
            # Assign all three stage controllers in a single tuple unpacking statement.
            # Python evaluates the entire right-hand side before performing any name
            # binding, so an exception raised during construction of any one stage
            # leaves the existing self.translator/planner/summarizer untouched.
            # Three separate assignments would risk a half-reloaded state if the
            # second or third constructor raised (e.g. bad env var for planner only).
            self.translator, self.planner, self.summarizer = (
                Translator(TranslatorConfig.from_env(), translator_backend, self.trace),
                Planner(PlannerConfig.from_env(),       planner_backend,    self.trace),
                Summarizer(SummarizerConfig.from_env(), summarizer_backend, self.trace),
            )
            # Only update self._cfg after all three constructors succeeded.
            # If any raised, self._cfg still points to the previous config so the
            # pipeline remains in a consistent state (controllers match config).
            self._cfg = new_cfg
            self._log("config_reloaded", {"msg": "Config reloaded from environment."})
        except Exception as exc:
            self._log("config_reload_failed", {"error": str(exc)})

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
                inner = raw.get("result", raw)
                payload = inner.get("payload", {}) if isinstance(inner, dict) else {}
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

    def _run_pipeline(self, req_id: int, user_text: str, strategy: str = "") -> PipelineResult:
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
        t_start = time.monotonic()
        t_translate = t_plan = t_execute = t_summarize = 0.0

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
        _t0 = time.monotonic()
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
        t_translate = (time.monotonic() - _t0) * 1000

        # Short-circuit: noop intent needs no plan or execution
        if intent.intent_type == "noop":
            self.trace.log({"event": "stage_timing", "req_id": req_id,
                            "translator_ms": round(t_translate, 1)})
            return PipelineResult(
                request_id=req_id, ts=ts, success=True,
                stage_failed=None, intent=intent, plan=None,
                step_results=[], human_summary=intent.human_summary,
                error=None, pipeline_build=PIPELINE_BUILD,
            )

        # Stage 2: Plan — IntentBlock → ordered ExecutionPlan of MCP tool calls
        _t0 = time.monotonic()
        try:
            plan = self.planner.plan(intent, req_id, strategy=strategy)
        except PlannerError as e:
            self.trace.log({"event": "stage_failed", "stage": "planner", "error": str(e)})
            return PipelineResult(
                request_id=req_id, ts=ts, success=False,
                stage_failed="planner", intent=intent, plan=None,
                step_results=[], human_summary=f"Failed to create execution plan: {e}",
                error=str(e), pipeline_build=PIPELINE_BUILD,
            )
        t_plan = (time.monotonic() - _t0) * 1000

        # Short-circuit: planner returned an empty steps list (unusual but valid)
        if not plan.steps:
            self.trace.log({"event": "stage_timing", "req_id": req_id,
                            "translator_ms": round(t_translate, 1),
                            "planner_ms": round(t_plan, 1)})
            return PipelineResult(
                request_id=req_id, ts=ts, success=True,
                stage_failed=None, intent=intent, plan=plan,
                step_results=[], human_summary=intent.human_summary,
                error=None, pipeline_build=PIPELINE_BUILD,
            )

        # Stage 3: Execute — run each plan step against the MCP server sequentially
        # ExecutorError carries partial_results so we can report what succeeded
        # before the failure, even when raising.
        _t0 = time.monotonic()
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
        t_execute = (time.monotonic() - _t0) * 1000

        # all_ok: every step either succeeded (ok=True) or was intentionally
        # skipped (on_error=skip). A skipped step doesn't count as failure.
        all_ok = all(r.ok or r.skipped for r in step_results)

        # Stage 4: Summarize — LLM produces a human-readable answer from tool results
        # Only runs when all steps passed; otherwise falls back to the intent's
        # pre-computed human_summary string.
        #
        # Streaming: tokens are written to runtime/agent/stream.jsonl as they
        # arrive. The UI server tails this file via /api/tokens SSE and delivers
        # them to the browser in near-real-time. stream_end is written in a
        # finally block so the SSE client always sees the end marker.
        _t0 = time.monotonic()
        if all_ok and step_results:
            stream_path = _runtime_agent_dir() / "stream.jsonl"

            # Overwrite (not append) to clear any previous query's tokens.
            # The SSE client seeks to end-of-file on connect, but clearing here
            # ensures stale tokens from a prior query never bleed into a new one
            # if the client reconnects mid-stream.
            try:
                stream_path.write_text(
                    json.dumps({"event": "stream_start", "req_id": req_id, "ts": int(time.time())}) + "\n",
                    encoding="utf-8",
                )
            except Exception as _stream_err:
                # Log instead of silently swallowing — a write failure here
                # (disk full, permissions) means no tokens will appear in the UI.
                # The summarizer still runs and its result still reaches the UI
                # via the outbox; streaming is best-effort, never blocks the result.
                self.trace.log({
                    "event": "stream_write_error",
                    "stage": "summarizer",
                    "req_id": req_id,
                    "error": str(_stream_err),
                })

            _token_write_failed = False

            def _on_token(text: str) -> None:
                # Called for each token chunk yielded by the LLM during streaming.
                # Each chunk is a separate JSONL line so the SSE endpoint can deliver
                # them individually as they arrive (tail-and-emit pattern).
                # Flush after every write so the OS buffer doesn't delay delivery.
                nonlocal _token_write_failed
                if _token_write_failed:
                    return
                try:
                    with stream_path.open("a", encoding="utf-8") as _sf:
                        _sf.write(json.dumps({"event": "token", "text": text}) + "\n")
                        _sf.flush()
                except Exception as _token_err:
                    # Log only the first failure — disk full or permissions errors
                    # repeat for every token and would flood the trace log.
                    _token_write_failed = True
                    self.trace.log({
                        "event": "stream_write_error",
                        "stage": "summarizer",
                        "req_id": req_id,
                        "error": str(_token_err),
                    })

            try:
                summary = self.summarizer.summarize(intent, step_results, req_id, on_token=_on_token)
            except Exception as e:
                # Summarizer failure is non-fatal — always fall back to the Translator's
                # pre-computed human_summary. Log the exception so it's visible in the trace.
                _token_write_failed = True  # suppress further token writes after the failure
                self.trace.log({"event": "summarizer_error", "req_id": req_id, "error": str(e)})
                summary = intent.human_summary
            finally:
                # Always write stream_end, even if the summarizer raised or if
                # earlier writes failed. The SSE client uses stream_end to remove
                # the blinking cursor; without it the cursor stays on screen forever.
                try:
                    with stream_path.open("a", encoding="utf-8") as _sf:
                        _sf.write(json.dumps({"event": "stream_end", "req_id": req_id, "ts": int(time.time())}) + "\n")
                        _sf.flush()
                except Exception as _end_err:
                    self.trace.log({
                        "event": "stream_write_error",
                        "stage": "summarizer",
                        "req_id": req_id,
                        "error": str(_end_err),
                    })
        else:
            summary = intent.human_summary
        t_summarize = (time.monotonic() - _t0) * 1000

        # Log per-stage timing for metrics aggregation (item 4)
        self.trace.log({
            "event": "stage_timing",
            "req_id": req_id,
            "translator_ms": round(t_translate, 1),
            "planner_ms":    round(t_plan, 1),
            "executor_ms":   round(t_execute, 1),
            "summarizer_ms": round(t_summarize, 1),
            "total_ms":      round((time.monotonic() - t_start) * 1000, 1),
        })

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
                # Check for pending config reload (triggered by SIGHUP)
                if self._reload_pending:
                    self._reload_config()

                for msg in read_new():
                    try:
                        req_id = int(msg.get("id", 0))
                    except (ValueError, TypeError):
                        req_id = 0
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
                        strategy = str(meta.get("strategy", "")) or self._cfg.default_payment_strategy
                        result = self._run_pipeline(req_id, user_text=user_text, strategy=strategy)
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
                        # Skip when translation failed (result.intent is None) —
                        # storing the error message would pollute the LLM's context
                        # and confuse subsequent queries.
                        if result.intent:
                            self._update_history(
                                user_text,
                                result.intent.goal,
                                outcome=arch_status,
                                human_summary=result.human_summary,
                            )

                    elif kind == "route":
                        # Inter-agent routing: forward the payload to another
                        # registered pipeline or agent process.
                        # meta must include: target_kind, target_node, and
                        # the message to forward as meta.payload.
                        target_kind = meta.get("target_kind", "pipeline")
                        try:
                            target_node = int(meta.get("target_node", 1))
                        except (ValueError, TypeError):
                            target_node = 1
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
                import io as _io
                _tb = _io.StringIO()
                traceback.print_exc(file=_tb)
                self._log("pipeline_error", {"traceback": _tb.getvalue().strip()})
                traceback.print_exc()

            # Drift-free sleep: targets the next absolute tick time so slow
            # queries don't cause cumulative drift in the inbox poll cadence.
            self._sched.wait_next_tick()


if __name__ == "__main__":
    PipelineCoordinator().run()
