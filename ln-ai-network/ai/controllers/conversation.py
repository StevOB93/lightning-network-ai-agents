from __future__ import annotations

# =============================================================================
# ConversationController — multi-turn LLM + MCP tool loop
#
# Extracted from LightningAgent._handle_freeform_llm() to make the core
# conversation logic independently testable and reusable.
#
# Responsibilities:
#   - Drive the LLM ↔ MCP tool loop until goal met or a safety limit fires
#   - Enforce five safety invariants (redundant recall, oscillation, read-only
#     cap, tool refusal, max steps)
#   - Run goal verification for payment-related requests
#   - Archive the trace on every exit path
#
# Not responsible for:
#   - Inbox/outbox file I/O (the caller writes reports)
#   - Process registration / startup locking (LightningAgent handles those)
#   - Rate limiting beyond the per-call min_interval_s check (GuardedBackend
#     handles RPM/TPM/circuit-breaker via the LLMBackend passed in)
# =============================================================================

import json
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from ai.llm.base import LLMRequest
from ai.tools import (
    FALLBACK_ALLOWED_TOOLS,
    READ_ONLY_TOOLS,
    STATE_CHANGING_TOOLS,
    _is_tool_error,
    _normalize_tool_args,
    _summarize_tool_result,
    _tool_sig,
    _try_parse_tool_call,
    llm_tools_schema,
)
from ai.utils import TraceLogger, _now_monotonic


# =============================================================================
# Configuration
# =============================================================================

@dataclass(frozen=True)
class ConversationConfig:
    """
    Immutable configuration for one ConversationController instance.

    allow_llm            — master kill switch; False → reject all queries
    max_steps            — maximum LLM iterations per request
    max_output_tokens    — hard cap on each LLM response length
    temperature          — sampling temperature for all calls
    goal_verify_enabled  — run _verify_payment_readiness() before accepting done
    min_llm_interval_s   — minimum seconds between consecutive LLM calls
    """
    allow_llm: bool = False
    max_steps: int = 60
    max_output_tokens: int = 900
    temperature: float = 0.2
    goal_verify_enabled: bool = True
    min_llm_interval_s: float = 1.0


# =============================================================================
# Helpers
# =============================================================================

def _text_wants_payment_flow(user_text: str) -> bool:
    """Return True if the request appears to involve a Lightning payment."""
    t = (user_text or "").lower()
    return any(k in t for k in ["pay", "invoice", "payment", "end-to-end", "end to end", "x402"])


def _json_only_requested(user_text: str) -> bool:
    """Return True if the user asked for JSON-only output."""
    t = (user_text or "").lower()
    return ("json only" in t) or ("strict json" in t) or ("return only one json" in t)


def _safe_get(d: Any, *keys: str, default: Any = None) -> Any:
    """Navigate a chain of dict keys without raising on missing keys or wrong types."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


# =============================================================================
# Controller
# =============================================================================

class ConversationController:
    """
    Drives a multi-turn LLM + MCP tool loop for a single user request.

    Usage:
        ctrl = ConversationController(cfg, backend, mcp, trace)
        ctrl.handle(req_id, user_text, on_report=write_report_fn)

    The `on_report` callback is called exactly once per handle() invocation
    with (req_id, content_str, optional_extra_dict).

    Safety limits (all trigger immediate return with a STOPPED message):
      1. Redundant read-only recall — same read-only sig since last state change
      2. Oscillation             — ≤2 unique read-only sigs in last 8 calls
      3. Too many consecutive read-only calls (>8 without a state change)
      4. LLM refuses tools despite require_tool_next (after 1 retry)
      5. Max steps exceeded
    """

    def __init__(
        self,
        config: ConversationConfig,
        backend: Any,      # LLMBackend — typed as Any to avoid import cycle
        mcp: Any,          # MCPClient
        trace: TraceLogger,
    ) -> None:
        self._cfg = config
        self._backend = backend
        self._mcp = mcp
        self._trace = trace
        self._next_llm_time = _now_monotonic()

    # ── Rate limiter ──────────────────────────────────────────────────────────

    def _llm_allowed(self) -> bool:
        return _now_monotonic() >= self._next_llm_time

    def _reserve_llm(self) -> None:
        self._next_llm_time = max(
            self._next_llm_time,
            _now_monotonic() + self._cfg.min_llm_interval_s,
        )

    # ── Tool execution helper ─────────────────────────────────────────────────

    def _call_tool_traced(
        self,
        req_id: int,
        name: str,
        args: Any,
        source: str,
        tool_calls_made: List[str],
    ) -> Dict[str, Any]:
        """
        Normalize args, call the MCP tool, and log both to trace.

        Returns the raw MCP result dict. On arg validation failure, returns
        {"id": 0, "error": "<message>"} in the MCP error shape.
        """
        norm_args, norm_err, changed = _normalize_tool_args(name, args)

        if changed:
            self._trace.log({
                "event": "tool_args_normalized",
                "tool": name, "source": source,
                "before": args, "after": norm_args,
            })

        if norm_err:
            self._trace.log({
                "event": "tool_args_invalid",
                "tool": name, "source": source,
                "error": norm_err, "args": norm_args,
            })
            return {"id": 0, "error": norm_err}

        sig = _tool_sig(name, norm_args)
        tool_calls_made.append(name)
        self._trace.log({
            "event": "tool_call", "source": source,
            "tool": name, "args": norm_args, "sig": sig,
        })

        result = self._mcp.call(name, args=norm_args)
        err = _is_tool_error(result)
        self._trace.log({
            "event": "tool_result", "source": source,
            "tool": name, "sig": sig, "ok": err is None,
            "error": err, "result_summary": _summarize_tool_result(result),
            "raw_result": result,
        })
        return result

    # ── Goal verification ─────────────────────────────────────────────────────

    def _verify_payment_readiness(
        self,
        req_id: int,
        tool_calls_made: List[str],
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Check that the Lightning network is ready for a payment flow.

        Returns (ok, reason, details).
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

    # ── Main loop ─────────────────────────────────────────────────────────────

    def handle(
        self,
        req_id: int,
        user_text: str,
        on_report: Callable[[int, str, Optional[Dict[str, Any]]], None],
        build: str = "",
    ) -> None:
        """
        Process a freeform user request using a multi-step LLM + tool loop.

        Calls on_report exactly once when the loop terminates (success or any
        safety-limit stop). The build string is embedded in the trace header
        for log correlation.
        """
        start_ts = int(time.time())
        self._trace.reset({
            "ts": start_ts,
            "event": "prompt_start",
            "build": build,
            "request_id": req_id,
            "allow_llm": self._cfg.allow_llm,
            "max_steps_per_command": self._cfg.max_steps,
            "user_text": user_text,
        })

        if not self._cfg.allow_llm:
            self._trace.log({"event": "llm_disabled"})
            on_report(req_id, "LLM is disabled (ALLOW_LLM!=1).", None)
            self._trace.archive(req_id, start_ts, "failed")
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

        self._trace.log({"event": "system_prompt", "text": system_prompt})

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ]

        seen_since_state_change: set[str] = set()
        consecutive_read_only = 0
        MAX_CONSEC_READ_ONLY = 8
        recent_sigs: Deque[str] = deque(maxlen=12)
        require_tool_next = False
        refused_tool_count = 0
        MAX_REFUSED_TOOL = 1

        steps = 0
        while steps < self._cfg.max_steps:
            if not self._llm_allowed():
                time.sleep(0.05)
                continue
            steps += 1
            self._reserve_llm()

            self._trace.log({
                "event": "llm_step_begin", "llm_step": steps,
                "require_tool_next": require_tool_next,
            })

            req = LLMRequest(
                messages=messages,
                tools=tools,
                max_output_tokens=self._cfg.max_output_tokens,
                temperature=self._cfg.temperature,
            )
            resp = self._backend.step(req)

            self._trace.log({
                "event": "llm_step_response", "llm_step": steps,
                "resp_type": getattr(resp, "type", None),
                "content_preview": (getattr(resp, "content", "") or "")[:400],
                "reasoning_preview": (getattr(resp, "reasoning", "") or "")[:400],
            })

            # ── Tool-call branch ─────────────────────────────────────────────
            if resp.type == "tool_call":
                require_tool_next = False
                if any(tc.name in STATE_CHANGING_TOOLS for tc in resp.tool_calls):
                    refused_tool_count = 0

                for tc in resp.tool_calls:
                    name = tc.name
                    args = tc.args or {}

                    norm_args, norm_err, changed = _normalize_tool_args(name, args)
                    if changed:
                        self._trace.log({
                            "event": "tool_args_normalized", "tool": name,
                            "source": "llm", "before": args, "after": norm_args,
                        })
                    if norm_err:
                        msg = f"STOPPED: invalid tool args for {name}: {norm_err}"
                        self._trace.log({"event": "stop", "reason": "tool_args_invalid",
                                         "message": msg, "tool": name, "args": norm_args})
                        on_report(req_id, msg, {"used_llm": True, "steps": steps,
                                                "tool_calls": tool_calls_made})
                        self._trace.archive(req_id, start_ts, "failed")
                        return
                    args = norm_args

                    sig = _tool_sig(name, args)
                    self._trace.log({"event": "tool_requested", "llm_step": steps,
                                     "tool": name, "args": args, "sig": sig})

                    if name in READ_ONLY_TOOLS and sig in seen_since_state_change:
                        msg = f"STOPPED: blocked redundant tool recall (no state change): {name} args={args}"
                        self._trace.log({"event": "stop", "reason": "redundant_recall",
                                         "message": msg, "sig": sig})
                        on_report(req_id, msg, {"used_llm": True, "steps": steps,
                                                "tool_calls": tool_calls_made, "blocked_sig": sig})
                        self._trace.archive(req_id, start_ts, "failed")
                        return

                    tool_calls_made.append(name)
                    recent_sigs.append(sig)

                    if len(recent_sigs) >= 8:
                        window = list(recent_sigs)[-8:]
                        uniq = set(window)
                        if len(uniq) <= 2 and all(s.split(":", 1)[0] in READ_ONLY_TOOLS for s in uniq):
                            msg = "STOPPED: tool-call oscillation detected (<=2 unique read-only signatures repeating)."
                            self._trace.log({"event": "stop", "reason": "oscillation",
                                             "message": msg, "recent_sigs": list(recent_sigs)})
                            on_report(req_id, msg, {"used_llm": True, "steps": steps,
                                                    "tool_calls": tool_calls_made,
                                                    "recent_sigs": list(recent_sigs)})
                            self._trace.archive(req_id, start_ts, "failed")
                            return

                    result = self._mcp.call(name, args=args)
                    err = _is_tool_error(result)

                    self._trace.log({
                        "event": "tool_result", "llm_step": steps,
                        "tool": name, "sig": sig, "ok": err is None,
                        "error": err, "result_summary": _summarize_tool_result(result),
                        "raw_result": result,
                    })

                    messages.append({"role": "assistant", "content": resp.content or ""})
                    messages.append({"role": "tool", "name": name,
                                     "content": json.dumps(result, ensure_ascii=False)})

                    if err:
                        msg = f"STOPPED: tool error in {name}\nError: {err}"
                        self._trace.log({"event": "stop", "reason": "tool_error", "message": msg})
                        on_report(req_id,
                                  f"{msg}\n\nRaw tool result:\n{json.dumps(result, indent=2, ensure_ascii=False)}",
                                  {"used_llm": True, "steps": steps, "tool_calls": tool_calls_made})
                        self._trace.archive(req_id, start_ts, "failed")
                        return

                    seen_since_state_change.add(sig)

                    if name in READ_ONLY_TOOLS:
                        consecutive_read_only += 1
                        if consecutive_read_only > MAX_CONSEC_READ_ONLY:
                            msg = "STOPPED: too many read-only tool calls without a state-changing action (stuck observing)."
                            self._trace.log({"event": "stop", "reason": "too_many_read_only",
                                             "message": msg, "recent_sigs": list(recent_sigs)})
                            on_report(req_id, msg, {"used_llm": True, "steps": steps,
                                                    "tool_calls": tool_calls_made,
                                                    "recent_sigs": list(recent_sigs)})
                            self._trace.archive(req_id, start_ts, "failed")
                            return
                    else:
                        consecutive_read_only = 0

                    if name in STATE_CHANGING_TOOLS:
                        seen_since_state_change.clear()
                        consecutive_read_only = 0
                        self._trace.log({"event": "state_changed", "tool": name,
                                         "note": "cleared recall gate + reset read-only counter"})

                continue

            # ── Final-response branch ────────────────────────────────────────
            final_text = resp.content or ""
            self._trace.log({"event": "final_candidate", "llm_step": steps,
                              "text_preview": final_text[:800]})

            if require_tool_next:
                parsed = _try_parse_tool_call(final_text)
                self._trace.log({"event": "fallback_parse_attempt", "llm_step": steps,
                                  "text": final_text, "parsed": parsed})

                if parsed:
                    tname, targs = parsed
                    if tname in FALLBACK_ALLOWED_TOOLS:
                        norm_args, norm_err, changed = _normalize_tool_args(tname, targs)
                        if changed:
                            self._trace.log({"event": "tool_args_normalized", "tool": tname,
                                             "source": "fallback", "before": targs, "after": norm_args})
                        if norm_err:
                            msg = f"STOPPED: invalid fallback tool args for {tname}: {norm_err}"
                            self._trace.log({"event": "stop", "reason": "tool_args_invalid",
                                             "message": msg, "tool": tname, "args": norm_args})
                            on_report(req_id, msg, {"used_llm": True, "steps": steps,
                                                    "tool_calls": tool_calls_made})
                            self._trace.archive(req_id, start_ts, "failed")
                            return

                        sig = _tool_sig(tname, norm_args)
                        self._trace.log({"event": "fallback_tool_execute", "tool": tname,
                                         "args": norm_args, "sig": sig})
                        tool_calls_made.append(tname)

                        result = self._mcp.call(tname, args=norm_args)
                        err = _is_tool_error(result)
                        self._trace.log({"event": "fallback_tool_result", "tool": tname,
                                         "ok": err is None, "error": err, "raw_result": result})
                        if err:
                            msg = f"STOPPED: tool error in {tname}\nError: {err}"
                            on_report(req_id,
                                      f"{msg}\n\nRaw tool result:\n{json.dumps(result, indent=2, ensure_ascii=False)}",
                                      {"used_llm": True, "steps": steps, "tool_calls": tool_calls_made})
                            self._trace.archive(req_id, start_ts, "failed")
                            return
                        continue

                refused_tool_count += 1
                self._trace.log({"event": "refused_tools", "count": refused_tool_count})
                if refused_tool_count > MAX_REFUSED_TOOL:
                    msg = "STOPPED: LLM refused to call tools while goal unmet."
                    self._trace.log({"event": "stop", "reason": "llm_refused_tools", "message": msg})
                    on_report(req_id, msg, {"used_llm": True, "steps": steps,
                                            "tool_calls": tool_calls_made})
                    self._trace.archive(req_id, start_ts, "failed")
                    return

            # ── Goal verification ────────────────────────────────────────────
            if self._cfg.goal_verify_enabled and wants_pay_flow:
                ok, reason, details = self._verify_payment_readiness(req_id, tool_calls_made)
                self._trace.log({"event": "goal_verify", "ok": ok, "reason": reason,
                                  "details": details})

                if not ok:
                    require_tool_next = True
                    fix_msg = (
                        "GOAL NOT MET. You MUST make MCP tool calls to fix this now.\n"
                        f"Blocker: {reason}\n"
                        "Next actions (execute):\n"
                        "- If peers are missing: ln_getinfo(node=2) -> extract id+binding; "
                        "then ln_connect(from_node=1, peer_id=<id>, host=<host>, port=<port>).\n"
                        "- Verify: ln_listpeers(node=1).\n"
                        "Do NOT respond with plain text. Respond with tool calls."
                    )
                    if final_text.strip():
                        messages.append({"role": "assistant", "content": final_text})
                    messages.append({"role": "user", "content": fix_msg})
                    self._trace.log({"event": "forced_tools_injected", "text": fix_msg})
                    continue

            # ── Success ──────────────────────────────────────────────────────
            self._trace.log({"event": "prompt_done", "llm_step": steps})
            on_report(req_id, final_text, {"used_llm": True, "steps": steps,
                                           "tool_calls": tool_calls_made})
            self._trace.archive(req_id, start_ts, "ok")
            return

        # Exceeded max_steps
        msg = "ERROR: exceeded max steps."
        self._trace.log({"event": "stop", "reason": "max_steps", "message": msg,
                          "tool_calls": tool_calls_made})
        on_report(req_id, msg, {"used_llm": True, "steps": steps,
                                "tool_calls": tool_calls_made})
        self._trace.archive(req_id, start_ts, "failed")
