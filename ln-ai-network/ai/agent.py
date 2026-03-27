from __future__ import annotations

# =============================================================================
# Legacy single-agent mode (ai.agent)
#
# This module implements the original, single-LLM-loop agent that was the
# precursor to the 4-stage pipeline in ai.pipeline. It is kept for backward
# compatibility and as a fallback when the pipeline is not configured.
#
# Architecture overview:
#   Inbox (JSONL) ──► run() loop ──► ConversationController.handle() ──► LLM
#      │                                         │                         │
#      │                                         ▼                         │ tool_calls
#      │                                  trace.log (JSONL)                │
#      │                                                                    ▼
#      └── write_outbox() ◄────────── on_report callback ◄──── MCP tool calls
#
# The multi-turn LLM+MCP loop and all five safety mechanisms live in
# ai.controllers.conversation.ConversationController. LightningAgent is now
# a thin process shell: startup, inbox polling, and report writing.
# =============================================================================

import json
import signal
import time
import traceback
from typing import Any, Dict, Optional

from ai.command_queue import read_new, write_outbox
from ai.controllers.conversation import ConversationConfig, ConversationController
from ai.core.registry import AgentRegistry
from ai.llm.factory import create_backend
from ai.mcp_client import FastMCPClientWrapper, MCPClient
from mcp.client.fastmcp import FastMCPClient
from ai.utils import (
    StartupLock,
    TraceLogger,
    _env_bool,
    _env_float,
    _env_int,
    _now_monotonic,
    _repo_root,
    _runtime_agent_dir,
)


# Version string embedded in trace headers so log analysis can correlate
# behaviour changes to code versions without needing git history.
AGENT_BUILD = "clean-single-agent-v5(trace+recall+oscillation+require-tools+fallback-parse+arg-normalize)"


# =============================================================================
# Agent
# =============================================================================

class LightningAgent:
    """
    Single-agent mode: one LLM loop handles an entire user request by issuing
    MCP tool calls until the goal is met or a safety limit triggers.

    Compare to PipelineCoordinator (ai.pipeline) which decomposes each request
    into four sequential stages (Translator → Planner → Executor → Summarizer).
    This agent uses a single conversational LLM loop instead.

    The multi-turn LLM+MCP loop lives in ConversationController. LightningAgent
    is a thin process shell: startup locking, inbox polling, and report writing.

    Initialization:
      - Acquires startup lock (enforces single-instance)
      - Connects to MCP server (FastMCPClientWrapper)
      - Creates LLM backend via create_backend() (reads ANTHROPIC_API_KEY etc.)
      - Builds ConversationController with config from env vars

    Main loop (run()):
      Polls inbox every tick_s seconds (default 500 ms).
      Dispatches 'freeform' messages with use_llm=True to _controller.handle().
    """

    def __init__(self) -> None:
        repo_root = _repo_root()
        lock_path = repo_root / "runtime" / "agent" / "agent.lock"
        self._lock = StartupLock(lock_path, name="agent")
        self._lock.acquire_or_exit()

        # Agent registry — register this process and clean up stale entries.
        self._registry = AgentRegistry(_repo_root() / "runtime" / "registry.jsonl")
        self._registry.purge_stale()
        self._node = _env_int("NODE_NUMBER", 1)
        self._registry.register(
            "agent", node=self._node,
            inbox_path=_runtime_agent_dir() / "inbox.jsonl",
        )

        # Hot-reload support: SIGHUP re-reads env vars and rebuilds the controller.
        self._reload_pending = False
        try:
            signal.signal(signal.SIGHUP, lambda s, f: setattr(self, "_reload_pending", True))
        except (OSError, ValueError):
            pass

        # MCP connection: FastMCPClientWrapper wraps the synchronous MCP client
        # so it can be called without async/await from a synchronous agent loop.
        self.mcp: MCPClient = FastMCPClientWrapper(FastMCPClient())
        self.backend = create_backend()

        # Poll interval: how long to sleep between inbox checks when idle
        self.tick_s = float(_env_int("AGENT_TICK_MS", 500)) / 1000.0

        # Shared trace logger: reset at start of each request, archived at end
        self.trace = TraceLogger(_runtime_agent_dir() / "trace.log")

        # Build the conversation controller from env vars
        self._controller = self._make_controller()

    def _make_controller(self) -> ConversationController:
        """Build a ConversationController from current environment variables."""
        cfg = ConversationConfig(
            allow_llm=_env_bool("ALLOW_LLM", default=False),
            max_steps=_env_int("LLM_MAX_STEPS_PER_COMMAND", 60),
            max_output_tokens=_env_int("LLM_MAX_OUTPUT_TOKENS", 900),
            temperature=_env_float("LLM_TEMPERATURE", 0.2),
            goal_verify_enabled=_env_bool("GOAL_VERIFY", default=True),
            min_llm_interval_s=float(_env_int("LLM_MIN_INTERVAL_MS", 1000)) / 1000.0,
        )
        return ConversationController(cfg, self.backend, self.mcp, self.trace)

    # -------------------------------------------------------------------------
    # Internal I/O helpers
    # -------------------------------------------------------------------------

    def _log(self, kind: str, payload: Dict[str, Any]) -> None:
        """Write a structured JSON line to stdout (visible in the terminal log)."""
        out = {"ts": int(time.time()), "kind": kind, **payload}
        print(json.dumps(out, ensure_ascii=False), flush=True)

    def _sleep_to_next_tick(self, start_t: float) -> None:
        """Sleep for whatever remains of the current tick budget."""
        elapsed = _now_monotonic() - start_t
        time.sleep(max(0.0, self.tick_s - elapsed))

    def _write_report(self, req_id: int, content: str, extra: Optional[Dict[str, Any]] = None) -> None:
        """
        Write an agent_report entry to the outbox (runtime/agent/outbox.jsonl).

        The UI server polls this file and pushes a pipeline_result SSE event
        to connected browsers when it changes.
        """
        entry: Dict[str, Any] = {
            "ts": int(time.time()),
            "type": "agent_report",
            "request_id": req_id,
            "content": content,
        }
        if extra:
            entry.update(extra)
        write_outbox(entry)

    # -------------------------------------------------------------------------
    # Main loop
    # -------------------------------------------------------------------------

    def run(self) -> None:
        """
        Poll the inbox indefinitely, dispatching messages to the appropriate handler.

        Message routing:
          kind="freeform" + use_llm=True  → _controller.handle()
          kind="health_check"              → write_report("Agent is running.")
          anything else                    → write_report("Unknown/unsupported command kind")

        Tick timing:
          Each iteration targets tick_s seconds (default 500 ms). If message
          processing takes longer, the next tick fires immediately.

        Error handling:
          KeyboardInterrupt → clean shutdown
          Any other exception → logged to stdout, traceback to stderr, loop continues
          (Keeps the agent alive through transient MCP/LLM errors)
        """
        self._log("agent_start", {
            "msg": "Agent online (single instance). Waiting for inbox commands.",
            "build": AGENT_BUILD,
        })

        while True:
            tick_start = _now_monotonic()
            try:
                # Hot-reload: SIGHUP handler sets _reload_pending; rebuild controller here
                # so the signal doesn't interrupt a live LLM call.
                if self._reload_pending:
                    self._reload_pending = False
                    self._controller = self._make_controller()
                    self._log("config_reloaded", {"msg": "Config reloaded from environment."})

                msgs = read_new()
                if not msgs:
                    self._sleep_to_next_tick(tick_start)
                    continue

                for msg in msgs:
                    req_id = int(msg.get("id", 0))
                    meta = msg.get("meta") or {}
                    kind = meta.get("kind")

                    if kind == "freeform" and bool(meta.get("use_llm", False)):
                        self._controller.handle(
                            req_id,
                            user_text=str(msg.get("content", "")),
                            on_report=self._write_report,
                            build=AGENT_BUILD,
                        )
                    elif kind == "health_check":
                        self._write_report(req_id, "Agent is running.", extra={"success": True})
                    else:
                        self._write_report(req_id, f"Unknown/unsupported command kind: {kind}")

                self._sleep_to_next_tick(tick_start)

            except KeyboardInterrupt:
                self._log("agent_stop", {"msg": "Shutdown requested."})
                break
            except Exception:
                self._log("agent_error", {})
                traceback.print_exc()
                self._sleep_to_next_tick(tick_start)


if __name__ == "__main__":
    LightningAgent().run()
