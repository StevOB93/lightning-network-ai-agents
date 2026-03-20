from __future__ import annotations

# =============================================================================
# Legacy single-agent mode (ai.agent)
#
# This module implements the original, single-LLM-loop agent that was the
# precursor to the 4-stage pipeline in ai.pipeline. It is kept for backward
# compatibility and as a fallback when the pipeline is not configured.
#
# Architecture overview:
#   Inbox (JSONL) ──► run() loop ──► _handle_freeform_llm() ──► LLM
#      │                                     │                    │
#      │                                     ▼                    │ tool_calls
#      │                              trace.log (JSONL)           │
#      │                                                          ▼
#      └── write_outbox() ◄──────── report ◄──────────── MCP tool calls
#
# Key safety mechanisms (all in _handle_freeform_llm):
#   1. Redundant-recall gate: blocks re-calling a read-only tool with the same
#      args unless a state-changing tool ran since (seen_since_state_change set).
#   2. Oscillation detector: if the last 8 tool calls use ≤2 unique read-only
#      signatures, the agent is stuck in a loop — stop immediately.
#   3. Consecutive read-only cap (MAX_CONSEC_READ_ONLY=8): prevents infinite
#      status-polling without ever taking an action.
#   4. Tool-refusal guard: if the LLM returns a final response when require_tool_next
#      is True, try the fallback parser; if that also fails, stop after 1 attempt.
#   5. Goal verification: for payment flows, _verify_payment_readiness() checks
#      nodes/peers/channels before accepting the LLM's "done" response; injects
#      a corrective user turn if the goal is not yet met.
# =============================================================================

import json
import os
import time
import traceback
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

from ai.command_queue import read_new, write_outbox
from ai.llm.base import LLMRequest
from ai.llm.factory import create_backend
from ai.mcp_client import FastMCPClientWrapper, MCPClient
from ai.tools import (
    # Tool-category sets used for enforcement policy decisions
    FALLBACK_ALLOWED_TOOLS,   # read-only ∪ state-changing — allowed via fallback parser
    READ_ONLY_TOOLS,          # tools that never mutate state (safe to gate on repeats)
    STATE_CHANGING_TOOLS,     # tools that mutate state (reset recall/oscillation gates)
    TOOL_REQUIRED,            # {tool_name: [required_arg_names]} registry

    # Utility functions — all shared with pipeline stages
    _coerce_int_fields,       # "1" → 1 for known integer fields
    _is_tool_error,           # extract error string from nested MCP result shape
    _normalize_tool_args,     # unwrap, coerce, validate (incl. node-range check)
    _summarize_tool_result,   # compact one-line summary for trace logs
    _tool_sig,                # deterministic "name:{...sorted-json...}" fingerprint
    _try_parse_tool_call,     # fallback text parser for when the LLM skips tool-use format

    # Schema builder for the LLM tool-use context window
    llm_tools_schema,
)
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
# Misc helpers
# =============================================================================

def _safe_get(d: Any, *keys: str, default: Any = None) -> Any:
    """
    Navigate a chain of dict keys without raising on missing keys or wrong types.

    Example:
      _safe_get(result, "result", "payload", "running", default=False)
      # → result["result"]["payload"]["running"] or False if any key is absent
    """
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _text_wants_payment_flow(user_text: str) -> bool:
    """
    Heuristic: does the user's request involve a Lightning payment?

    When True, goal verification (_verify_payment_readiness) is applied before
    accepting the LLM's final response, ensuring nodes/peers/channels are ready.
    """
    t = (user_text or "").lower()
    keywords = ["pay", "invoice", "payment", "end-to-end", "end to end", "x402"]
    return any(k in t for k in keywords)


def _json_only_requested(user_text: str) -> bool:
    """
    Heuristic: did the user ask for JSON-only output?

    If True, 'Return JSON only.' is appended to the system prompt so the LLM
    avoids prose wrapping around tool results.
    """
    t = (user_text or "").lower()
    return ("json only" in t) or ("strict json" in t) or ("return only one json" in t)


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

    Initialization:
      - Acquires startup lock (enforces single-instance)
      - Connects to MCP server (FastMCPClientWrapper)
      - Creates LLM backend via create_backend() (reads ANTHROPIC_API_KEY etc.)
      - Reads all config from env vars (AGENT_TICK_MS, ALLOW_LLM, etc.)

    Main loop (run()):
      Polls inbox every tick_s seconds (default 500 ms).
      Dispatches 'freeform' messages with use_llm=True to _handle_freeform_llm().
    """

    def __init__(self) -> None:
        repo_root = _repo_root()
        lock_path = repo_root / "runtime" / "agent" / "agent.lock"
        self._lock = StartupLock(lock_path, name="agent")
        self._lock.acquire_or_exit()

        # MCP connection: FastMCPClientWrapper wraps the synchronous MCP client
        # so it can be called without async/await from a synchronous agent loop.
        self.mcp: MCPClient = FastMCPClientWrapper(FastMCPClient())
        self.backend = create_backend()

        # Poll interval: how long to sleep between inbox checks when idle
        self.tick_s = float(_env_int("AGENT_TICK_MS", 500)) / 1000.0

        # LLM controls
        # ALLOW_LLM=0 disables all LLM calls; the agent will only respond to
        # health_check messages. Useful for testing the inbox/outbox plumbing.
        self.allow_llm = _env_bool("ALLOW_LLM", default=False)
        # Minimum wall-clock gap between LLM calls — rate-limits to avoid
        # runaway billing during rapid re-submissions.
        self.min_llm_interval_s = float(_env_int("LLM_MIN_INTERVAL_MS", 1000)) / 1000.0
        self._next_llm_time = _now_monotonic()  # becomes "now" on first call

        # Per-request step budget: each LLM iteration (call + tool calls) is one step.
        self.max_steps_per_command = _env_int("LLM_MAX_STEPS_PER_COMMAND", 60)
        self.llm_max_output_tokens = _env_int("LLM_MAX_OUTPUT_TOKENS", 900)
        self.llm_temperature = _env_float("LLM_TEMPERATURE", 0.2)

        # Goal verification: when enabled and the request looks like a payment flow,
        # _verify_payment_readiness() is called before accepting the LLM's "done".
        self.goal_verify_enabled = _env_bool("GOAL_VERIFY", default=True)

        # Shared trace logger: reset at start of each request, archived at end
        self.trace = TraceLogger(_runtime_agent_dir() / "trace.log")

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
    # LLM rate limiter
    # -------------------------------------------------------------------------

    def _llm_allowed(self) -> bool:
        """True if enough time has passed since the last LLM call."""
        return _now_monotonic() >= self._next_llm_time

    def _reserve_llm(self) -> None:
        """Advance the next-allowed-LLM time by min_llm_interval_s."""
        self._next_llm_time = max(self._next_llm_time, _now_monotonic() + self.min_llm_interval_s)

    # -------------------------------------------------------------------------
    # Centralized MCP call with normalization + trace
    # -------------------------------------------------------------------------

    def _call_tool_traced(
        self,
        req_id: int,
        name: str,
        args: Any,
        source: str,
        tool_calls_made: List[str],
    ) -> Dict[str, Any]:
        """
        Normalize args, call the MCP tool, and log both the call and result to trace.

        Parameters:
          name            — tool name (must be in TOOL_REQUIRED or normalization is skipped)
          args            — raw args dict (may be un-normalized / wrapped in {"args": {...}})
          source          — label for the trace ("llm", "verify", "fallback")
          tool_calls_made — accumulator list; tool name is appended here (for report metadata)

        Returns the raw MCP result dict. On arg validation failure, returns
        {"id": 0, "error": "<message>"} in the same shape as an MCP error so
        callers can handle both paths identically.

        Note: This helper is used by _verify_payment_readiness(). The main LLM
        tool-call loop in _handle_freeform_llm() inlines its own normalization
        to handle the oscillation/recall gates before touching the MCP.
        """
        norm_args, norm_err, changed = _normalize_tool_args(name, args)

        if changed:
            self.trace.log({
                "event": "tool_args_normalized",
                "tool": name,
                "source": source,
                "before": args,
                "after": norm_args,
            })

        if norm_err:
            self.trace.log({
                "event": "tool_args_invalid",
                "tool": name,
                "source": source,
                "error": norm_err,
                "args": norm_args,
            })
            # Return in the MCP error shape so callers can use _is_tool_error() uniformly
            return {"id": 0, "error": norm_err}

        sig = _tool_sig(name, norm_args)
        tool_calls_made.append(name)
        self.trace.log({
            "event": "tool_call",
            "source": source,
            "tool": name,
            "args": norm_args,
            "sig": sig,
        })

        result = self.mcp.call(name, args=norm_args)
        err = _is_tool_error(result)

        self.trace.log({
            "event": "tool_result",
            "source": source,
            "tool": name,
            "sig": sig,
            "ok": err is None,
            "error": err,
            "result_summary": _summarize_tool_result(result),
            "raw_result": result,
        })
        return result

    # -------------------------------------------------------------------------
    # Goal verification
    # -------------------------------------------------------------------------

    def _verify_payment_readiness(
        self,
        req_id: int,
        tool_calls_made: List[str],
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Verify that the Lightning network is ready for a payment flow.

        Checks (in order):
          1. Both node-1 and node-2 are running (ln_node_status)
          2. Node-1 has at least one connected peer (ln_listpeers)
          3. Node-1 has at least one channel (ln_listfunds)

        Returns (ok, reason, details):
          ok      — True if all checks pass
          reason  — human-readable explanation of the first failing check
          details — raw tool results for the trace log

        Called in the "final" branch of the LLM loop after the LLM thinks it
        is done. If ok=False, the loop injects a corrective user turn telling
        the LLM exactly what to fix, and sets require_tool_next=True.
        """
        details: Dict[str, Any] = {}

        st1 = self._call_tool_traced(req_id, "ln_node_status", {"node": 1}, "verify", tool_calls_made)
        st2 = self._call_tool_traced(req_id, "ln_node_status", {"node": 2}, "verify", tool_calls_made)
        details["node_status"] = {"1": st1, "2": st2}

        r1 = _safe_get(st1, "result", "payload", "running", default=False)
        r2 = _safe_get(st2, "result", "payload", "running", default=False)
        if not (r1 and r2):
            return False, "Nodes not running (need node-1 and node-2 running).", details

        peers = self._call_tool_traced(req_id, "ln_listpeers", {"node": 1}, "verify", tool_calls_made)
        details["listpeers_1"] = peers
        p_payload = _safe_get(peers, "result", "payload", default={})
        peer_count = (
            len(p_payload.get("peers", []))
            if isinstance(p_payload, dict) and isinstance(p_payload.get("peers"), list)
            else 0
        )
        if peer_count < 1:
            return False, "No peers connected from node-1 (need at least 1 peer).", details

        funds = self._call_tool_traced(req_id, "ln_listfunds", {"node": 1}, "verify", tool_calls_made)
        details["listfunds_1"] = funds
        f_payload = _safe_get(funds, "result", "payload", default={})
        ch_count = (
            len(f_payload.get("channels", []))
            if isinstance(f_payload, dict) and isinstance(f_payload.get("channels"), list)
            else 0
        )
        if ch_count < 1:
            return False, "No channels found on node-1 (need at least 1 channel).", details

        return True, "Ready to attempt ln_invoice + ln_pay.", details

    # -------------------------------------------------------------------------
    # LLM orchestration loop
    # -------------------------------------------------------------------------

    def _handle_freeform_llm(self, req_id: int, user_text: str) -> None:
        """
        Process a freeform user request using a multi-step LLM + tool loop.

        Loop invariants:
          - messages is the full conversation context sent to the LLM each step.
          - Each tool call result is appended to messages (role=tool) so the LLM
            can see what happened and decide what to do next.
          - The loop terminates when: (a) the LLM returns a final text response
            and goal verification passes, (b) a safety limit fires, or (c) a tool
            error occurs. All termination paths call _write_report() exactly once.

        Safety limits (all trigger immediate return with a STOPPED message):
          - Redundant read-only recall: same read-only sig since last state change
          - Oscillation: ≤2 unique read-only sigs in last 8 calls
          - Too many consecutive read-only calls (>8)
          - LLM refuses tools despite require_tool_next flag (after 1 retry)
          - Max steps exceeded (LLM_MAX_STEPS_PER_COMMAND)
        """
        # Capture timestamp before reset() — used for the archive filename later
        start_ts = int(time.time())
        self.trace.reset({
            "ts": start_ts,
            "event": "prompt_start",
            "build": AGENT_BUILD,
            "request_id": req_id,
            "allow_llm": self.allow_llm,
            "max_steps_per_command": self.max_steps_per_command,
            "user_text": user_text,
        })

        if not self.allow_llm:
            self.trace.log({"event": "llm_disabled"})
            self._write_report(req_id, "LLM is disabled (ALLOW_LLM!=1).")
            self.trace.archive(req_id, start_ts, "failed")
            return

        tools = llm_tools_schema()
        tool_calls_made: List[str] = []
        json_only = _json_only_requested(user_text)
        wants_pay_flow = _text_wants_payment_flow(user_text)

        system_prompt = (
            "You are a Lightning Network controller running in regtest.\n"
            "You can ONLY act via the provided MCP tools.\n"
            "Stop immediately on any tool error.\n"
            "If goal verification fails, you MUST respond with tool calls to fix the blocker.\n"
            "Do NOT repeat the same read-only tool+args unless a state-changing tool succeeded since.\n"
            "IMPORTANT: When calling tools, pass ONLY the tool's required arguments at top-level.\n"
            "Do NOT wrap tool args inside {\"args\": {...}} or include extra fields like status/result.\n"
        )
        if json_only:
            system_prompt += "Return JSON only.\n"

        self.trace.log({"event": "system_prompt", "text": system_prompt})

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ]

        # ── Safety state ────────────────────────────────────────────────────
        # seen_since_state_change: tracks read-only tool signatures since the
        # last state-changing tool call. Cleared on every STATE_CHANGING_TOOLS
        # success. A repeat hit → stop (redundant recall gate).
        seen_since_state_change: set[str] = set()

        # consecutive_read_only: count of back-to-back read-only calls.
        # Reset to 0 after any state-changing tool. Capped at MAX_CONSEC_READ_ONLY.
        consecutive_read_only = 0
        MAX_CONSEC_READ_ONLY = 8

        # recent_sigs: sliding window of last 12 tool call signatures.
        # Used by the oscillation detector to spot stuck loops.
        recent_sigs: Deque[str] = deque(maxlen=12)

        # require_tool_next: set True when goal verification fails or when the
        # LLM returns a final text response prematurely. Forces the next loop
        # iteration to treat a non-tool response as a fallback-parse candidate.
        require_tool_next = False

        # refused_tool_count: incremented when the LLM returns a final response
        # despite require_tool_next. After MAX_REFUSED_TOOL attempts the loop stops.
        refused_tool_count = 0
        MAX_REFUSED_TOOL = 1

        steps = 0
        while steps < self.max_steps_per_command:
            # Rate-limit: spin-wait until the next LLM call is allowed
            if not self._llm_allowed():
                time.sleep(0.05)
                continue
            steps += 1
            self._reserve_llm()

            self.trace.log({
                "event": "llm_step_begin",
                "llm_step": steps,
                "require_tool_next": require_tool_next,
            })

            req = LLMRequest(
                messages=messages,
                tools=tools,
                max_output_tokens=self.llm_max_output_tokens,
                temperature=self.llm_temperature,
            )
            resp = self.backend.step(req)

            self.trace.log({
                "event": "llm_step_response",
                "llm_step": steps,
                "resp_type": getattr(resp, "type", None),
                "content_preview": (getattr(resp, "content", "") or "")[:400],
                "reasoning_preview": (getattr(resp, "reasoning", "") or "")[:400],
            })

            # ── Tool-call branch ─────────────────────────────────────────────
            if resp.type == "tool_call":
                require_tool_next = False
                # Only reset the refusal counter when the LLM issues a state-
                # changing call — a read-only call when require_tool_next was
                # set does not count as "complying" with the goal-fix directive.
                if any(tc.name in STATE_CHANGING_TOOLS for tc in resp.tool_calls):
                    refused_tool_count = 0

                for tc in resp.tool_calls:
                    name = tc.name
                    args = tc.args or {}

                    # ── Step 1: Normalize args ───────────────────────────────
                    # Do this BEFORE sig computation and gate checks so that
                    # wrapped args ({"args": {...}}) are always unwrapped first.
                    # _normalize_tool_args also validates node ranges and BTC
                    # address prefixes (additional safety vs the local copy it
                    # replaces).
                    norm_args, norm_err, changed = _normalize_tool_args(name, args)
                    if changed:
                        self.trace.log({
                            "event": "tool_args_normalized",
                            "tool": name,
                            "source": "llm",
                            "before": args,
                            "after": norm_args,
                        })
                    if norm_err:
                        msg = f"STOPPED: invalid tool args for {name}: {norm_err}"
                        self.trace.log({"event": "stop", "reason": "tool_args_invalid", "message": msg, "tool": name, "args": norm_args})
                        self._write_report(req_id, msg, extra={"used_llm": True, "steps": steps, "tool_calls": tool_calls_made})
                        self.trace.archive(req_id, start_ts, "failed")
                        return
                    args = norm_args

                    sig = _tool_sig(name, args)
                    self.trace.log({"event": "tool_requested", "llm_step": steps, "tool": name, "args": args, "sig": sig})

                    # ── Step 2: Redundant-recall gate ────────────────────────
                    # Prevent the LLM from re-reading the same state it already
                    # has without any intermediate mutation. Applies only to
                    # read-only tools (state-changing tools are always allowed).
                    if name in READ_ONLY_TOOLS and sig in seen_since_state_change:
                        msg = f"STOPPED: blocked redundant tool recall (no state change): {name} args={args}"
                        self.trace.log({"event": "stop", "reason": "redundant_recall", "message": msg, "sig": sig})
                        self._write_report(req_id, msg, extra={"used_llm": True, "steps": steps, "tool_calls": tool_calls_made, "blocked_sig": sig})
                        self.trace.archive(req_id, start_ts, "failed")
                        return

                    tool_calls_made.append(name)
                    recent_sigs.append(sig)

                    # ── Step 3: Oscillation detector ─────────────────────────
                    # After the window fills (8+ entries), check whether the
                    # last 8 calls are all read-only and use ≤2 unique signatures.
                    # This catches tight loops like: check_status → wait → check_status.
                    if len(recent_sigs) >= 8:
                        window = list(recent_sigs)[-8:]
                        uniq = set(window)
                        if len(uniq) <= 2 and all(s.split(":", 1)[0] in READ_ONLY_TOOLS for s in uniq):
                            msg = "STOPPED: tool-call oscillation detected (<=2 unique read-only signatures repeating)."
                            self.trace.log({"event": "stop", "reason": "oscillation", "message": msg, "recent_sigs": list(recent_sigs)})
                            self._write_report(req_id, msg, extra={"used_llm": True, "steps": steps, "tool_calls": tool_calls_made, "recent_sigs": list(recent_sigs)})
                            self.trace.archive(req_id, start_ts, "failed")
                            return

                    # ── Step 4: Execute the tool ─────────────────────────────
                    result = self.mcp.call(name, args=args)
                    err = _is_tool_error(result)

                    self.trace.log({
                        "event": "tool_result",
                        "llm_step": steps,
                        "tool": name,
                        "sig": sig,
                        "ok": err is None,
                        "error": err,
                        "result_summary": _summarize_tool_result(result),
                        "raw_result": result,
                    })

                    # Append to conversation so the LLM sees what happened
                    messages.append({"role": "assistant", "content": resp.content or ""})
                    messages.append({"role": "tool", "name": name, "content": json.dumps(result, ensure_ascii=False)})

                    if err:
                        msg = f"STOPPED: tool error in {name}\nError: {err}"
                        self.trace.log({"event": "stop", "reason": "tool_error", "message": msg})
                        self._write_report(
                            req_id,
                            f"{msg}\n\nRaw tool result:\n{json.dumps(result, indent=2, ensure_ascii=False)}",
                            extra={"used_llm": True, "steps": steps, "tool_calls": tool_calls_made},
                        )
                        self.trace.archive(req_id, start_ts, "failed")
                        return

                    # ── Step 5: Update safety counters ───────────────────────
                    seen_since_state_change.add(sig)

                    if name in READ_ONLY_TOOLS:
                        consecutive_read_only += 1
                        if consecutive_read_only > MAX_CONSEC_READ_ONLY:
                            msg = "STOPPED: too many read-only tool calls without a state-changing action (stuck observing)."
                            self.trace.log({"event": "stop", "reason": "too_many_read_only", "message": msg, "recent_sigs": list(recent_sigs)})
                            self._write_report(req_id, msg, extra={"used_llm": True, "steps": steps, "tool_calls": tool_calls_made, "recent_sigs": list(recent_sigs)})
                            self.trace.archive(req_id, start_ts, "failed")
                            return
                    else:
                        consecutive_read_only = 0

                    if name in STATE_CHANGING_TOOLS:
                        # A successful mutation resets the recall and read-only
                        # counters — the LLM is allowed to re-read state that
                        # may have changed as a result of the action.
                        seen_since_state_change.clear()
                        consecutive_read_only = 0
                        self.trace.log({"event": "state_changed", "tool": name, "note": "cleared recall gate + reset read-only counter"})

                continue  # Fetch next LLM response with updated message history

            # ── Final-response branch ────────────────────────────────────────
            # The LLM returned a plain text response (not tool_call).
            # This is either the genuine final answer or a premature "done".
            final_text = resp.content or ""
            self.trace.log({"event": "final_candidate", "llm_step": steps, "text_preview": final_text[:800]})

            if require_tool_next:
                # Goal verification (or a prior refusal) told us we need more
                # tool calls. Try to salvage by parsing the LLM's text as a
                # tool call in non-standard format (e.g. "ln_getinfo(node=1)").
                parsed = _try_parse_tool_call(final_text)
                self.trace.log({"event": "fallback_parse_attempt", "llm_step": steps, "text": final_text, "parsed": parsed})

                if parsed:
                    tname, targs = parsed
                    if tname in FALLBACK_ALLOWED_TOOLS:
                        norm_args, norm_err, changed = _normalize_tool_args(tname, targs)
                        if changed:
                            self.trace.log({"event": "tool_args_normalized", "tool": tname, "source": "fallback", "before": targs, "after": norm_args})
                        if norm_err:
                            msg = f"STOPPED: invalid fallback tool args for {tname}: {norm_err}"
                            self.trace.log({"event": "stop", "reason": "tool_args_invalid", "message": msg, "tool": tname, "args": norm_args})
                            self._write_report(req_id, msg, extra={"used_llm": True, "steps": steps, "tool_calls": tool_calls_made})
                            self.trace.archive(req_id, start_ts, "failed")
                            return

                        sig = _tool_sig(tname, norm_args)
                        self.trace.log({"event": "fallback_tool_execute", "tool": tname, "args": norm_args, "sig": sig})
                        tool_calls_made.append(tname)

                        result = self.mcp.call(tname, args=norm_args)
                        err = _is_tool_error(result)
                        self.trace.log({"event": "fallback_tool_result", "tool": tname, "ok": err is None, "error": err, "raw_result": result})
                        if err:
                            msg = f"STOPPED: tool error in {tname}\nError: {err}"
                            self._write_report(
                                req_id,
                                f"{msg}\n\nRaw tool result:\n{json.dumps(result, indent=2, ensure_ascii=False)}",
                                extra={"used_llm": True, "steps": steps, "tool_calls": tool_calls_made},
                            )
                            self.trace.archive(req_id, start_ts, "failed")
                            return
                        continue  # Resume loop with tool result appended

                # Fallback parse failed (or tool not in FALLBACK_ALLOWED_TOOLS)
                refused_tool_count += 1
                self.trace.log({"event": "refused_tools", "count": refused_tool_count})
                if refused_tool_count > MAX_REFUSED_TOOL:
                    msg = "STOPPED: LLM refused to call tools while goal unmet."
                    self.trace.log({"event": "stop", "reason": "llm_refused_tools", "message": msg})
                    self._write_report(req_id, msg, extra={"used_llm": True, "steps": steps, "tool_calls": tool_calls_made})
                    self.trace.archive(req_id, start_ts, "failed")
                    return

            # ── Goal verification ────────────────────────────────────────────
            # For payment-related requests, verify the network state is actually
            # ready before accepting this as the final answer. If not ready,
            # inject a corrective turn and loop back (require_tool_next=True).
            if self.goal_verify_enabled and wants_pay_flow:
                ok, reason, details = self._verify_payment_readiness(req_id, tool_calls_made)
                self.trace.log({"event": "goal_verify", "ok": ok, "reason": reason, "details": details})

                if not ok:
                    require_tool_next = True
                    fix_msg = (
                        "GOAL NOT MET. You MUST make MCP tool calls to fix this now.\n"
                        f"Blocker: {reason}\n"
                        "Next actions (execute):\n"
                        "- If peers are missing: ln_getinfo(node=2) -> extract id+binding; then ln_connect(from_node=1, peer_id=<id>, host=<host>, port=<port>).\n"
                        "- Verify: ln_listpeers(node=1).\n"
                        "Do NOT respond with plain text. Respond with tool calls."
                    )
                    # Only append the LLM's response if it had content — an empty
                    # assistant turn confuses some LLM backends.
                    if final_text.strip():
                        messages.append({"role": "assistant", "content": final_text})
                    messages.append({"role": "user", "content": fix_msg})
                    self.trace.log({"event": "forced_tools_injected", "text": fix_msg})
                    continue

            # ── Success ──────────────────────────────────────────────────────
            self.trace.log({"event": "prompt_done", "llm_step": steps})
            self._write_report(req_id, final_text, extra={"used_llm": True, "steps": steps, "tool_calls": tool_calls_made})
            self.trace.archive(req_id, start_ts, "ok")
            return

        # Exceeded max_steps — report failure and archive the trace
        msg = "ERROR: exceeded max steps."
        self.trace.log({"event": "stop", "reason": "max_steps", "message": msg, "tool_calls": tool_calls_made})
        self._write_report(req_id, msg, extra={"used_llm": True, "steps": steps, "tool_calls": tool_calls_made})
        self.trace.archive(req_id, start_ts, "failed")

    # -------------------------------------------------------------------------
    # Main loop
    # -------------------------------------------------------------------------

    def run(self) -> None:
        """
        Poll the inbox indefinitely, dispatching messages to the appropriate handler.

        Message routing:
          kind="freeform" + use_llm=True  → _handle_freeform_llm()
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
                msgs = read_new()
                if not msgs:
                    self._sleep_to_next_tick(tick_start)
                    continue

                for msg in msgs:
                    req_id = int(msg.get("id", 0))
                    meta = msg.get("meta") or {}
                    kind = meta.get("kind")

                    if kind == "freeform" and bool(meta.get("use_llm", False)):
                        self._handle_freeform_llm(req_id, user_text=str(msg.get("content", "")))
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
