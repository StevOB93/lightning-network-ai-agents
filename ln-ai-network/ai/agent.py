from __future__ import annotations

import atexit
import json
import os
import re
import sys
import time
import traceback
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

from ai.command_queue import read_new, write_outbox
from ai.llm.base import LLMRequest
from ai.llm.factory import create_backend
from ai.mcp_client import FastMCPClientWrapper, MCPClient
from mcp.client.fastmcp import FastMCPClient

try:
    import fcntl  # Linux/WSL
except Exception:
    fcntl = None  # type: ignore


AGENT_BUILD = "clean-single-agent-v5(trace+recall+oscillation+require-tools+fallback-parse+arg-normalize)"


# =============================================================================
# Startup lock (single agent instance)
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
                    msg = existing or "Another agent instance holds the lock."
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
                "kind": "agent_lock_failed",
                "lock_path": str(self.lock_path),
                "error": str(e),
                "hint": "Another ai.agent process is already running. Stop it before starting a new one.",
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
    """
    Writes JSONL to runtime/agent/trace.log.
    Resets (truncates) at the start of each prompt/command.
    """
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


def _env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None:
        return default
    try:
        return float(v.strip())
    except Exception:
        return default


def _safe_get(d: Any, *keys: str, default: Any = None) -> Any:
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _is_tool_error(result: Any) -> Optional[str]:
    if not isinstance(result, dict):
        return None

    if "error" in result and isinstance(result["error"], str) and result["error"].strip():
        return result["error"].strip()

    if result.get("ok") is False:
        err = result.get("error")
        return str(err) if err else "Tool returned ok=false"

    inner = result.get("result")
    if isinstance(inner, dict):
        if "error" in inner and isinstance(inner["error"], str) and inner["error"].strip():
            return inner["error"].strip()
        if inner.get("ok") is False:
            err = inner.get("error")
            return str(err) if err else "Tool returned ok=false"

    return None


def _text_wants_payment_flow(user_text: str) -> bool:
    t = (user_text or "").lower()
    keywords = ["pay", "invoice", "payment", "end-to-end", "end to end", "x402"]
    return any(k in t for k in keywords)


def _json_only_requested(user_text: str) -> bool:
    t = (user_text or "").lower()
    return ("json only" in t) or ("strict json" in t) or ("return only one json" in t)


def _tool_sig(name: str, args: Dict[str, Any]) -> str:
    try:
        return f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
    except Exception:
        return f"{name}:{str(args)}"


def _summarize_tool_result(result: Any, max_len: int = 400) -> str:
    try:
        if isinstance(result, dict):
            if "error" in result:
                return f"error={str(result.get('error'))[:max_len]}"
            inner = result.get("result")
            if isinstance(inner, dict):
                if inner.get("ok") is False:
                    return f"ok=false error={str(inner.get('error'))[:max_len]}"
                if inner.get("ok") is True:
                    payload = inner.get("payload")
                    if payload is None:
                        return "ok=true (no payload)"
                    s = json.dumps(payload, ensure_ascii=False)
                    return f"ok=true payload={s[:max_len]}"
            s = json.dumps(result, ensure_ascii=False)
            return s[:max_len]
        return str(result)[:max_len]
    except Exception:
        return "<unserializable>"


# =============================================================================
# Tool enforcement policy
# =============================================================================

READ_ONLY_TOOLS = {
    "network_health",
    "btc_getblockchaininfo",
    "btc_getnewaddress",
    "ln_listnodes",
    "ln_node_status",
    "ln_getinfo",
    "ln_listpeers",
    "ln_listfunds",
    "ln_listchannels",
    "ln_newaddr",
}

STATE_CHANGING_TOOLS = {
    "btc_wallet_ensure",
    "btc_sendtoaddress",
    "btc_generatetoaddress",
    "ln_node_create",
    "ln_node_start",
    "ln_node_stop",
    "ln_node_delete",
    "ln_connect",
    "ln_openchannel",
    "ln_invoice",
    "ln_pay",
}

FALLBACK_ALLOWED_TOOLS = READ_ONLY_TOOLS | STATE_CHANGING_TOOLS


# =============================================================================
# Deterministic tool arg normalization/validation
# =============================================================================

TOOL_REQUIRED: Dict[str, List[str]] = {
    # Health
    "network_health": [],

    # Bitcoin
    "btc_getblockchaininfo": [],
    "btc_wallet_ensure": ["wallet_name"],
    "btc_getnewaddress": [],
    "btc_sendtoaddress": ["address", "amount_btc"],  # wallet is optional in tool schema
    "btc_generatetoaddress": ["blocks", "address"],

    # Nodes
    "ln_listnodes": [],
    "ln_node_status": ["node"],
    "ln_node_start": ["node"],
    "ln_node_create": ["node"],
    "ln_node_stop": ["node"],
    "ln_node_delete": ["node"],

    # Lightning read
    "ln_getinfo": ["node"],
    "ln_listpeers": ["node"],
    "ln_listfunds": ["node"],
    "ln_listchannels": ["node"],
    "ln_newaddr": ["node"],

    # Lightning actions
    "ln_connect": ["from_node", "peer_id", "host", "port"],
    "ln_openchannel": ["from_node", "peer_id", "amount_sat"],

    # Payments
    "ln_invoice": ["node", "amount_msat", "label", "description"],
    "ln_pay": ["from_node", "bolt11"],
}

_INT_KEYS = {"node", "from_node", "port", "blocks", "amount_sat", "amount_msat"}


def _coerce_int_fields(args: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(args)
    for k, v in list(out.items()):
        if k in _INT_KEYS and isinstance(v, str):
            vs = v.strip()
            if vs.isdigit():
                out[k] = int(vs)
    return out


def _normalize_tool_args(tool: str, args: Any) -> Tuple[Dict[str, Any], Optional[str], bool]:
    """
    Returns (normalized_args, error_or_none, changed_bool).

    Fixes common LLM tool-call arg shapes:
      - unwraps {"args": {...}} if required keys are missing but nested args exists
      - merges nested args into top-level (nested wins)
      - coerces common integer fields
      - validates required keys (if known)
    """
    changed = False

    a: Dict[str, Any] = args if isinstance(args, dict) else {}
    reqs = TOOL_REQUIRED.get(tool)

    # If required keys are missing and there is an inner "args" dict, unwrap it.
    if reqs is not None and reqs:
        missing = [k for k in reqs if k not in a]
        if missing:
            inner = a.get("args")
            if isinstance(inner, dict):
                merged = dict(a)
                merged.pop("args", None)
                merged.update(inner)
                a = merged
                changed = True

    a2 = _coerce_int_fields(a)
    if a2 != a:
        changed = True
    a = a2

    if reqs is not None and reqs:
        missing2 = [k for k in reqs if k not in a]
        if missing2:
            return a, f"tool args missing required keys: {missing2}", changed

    return a, None, changed


# =============================================================================
# Fallback tool-call parsing (strict)
# =============================================================================

def _parse_value(s: str) -> Any:
    s = s.strip()
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    if s.lower() in ("null", "none"):
        return None
    if re.fullmatch(r"-?\d+", s):
        try:
            return int(s)
        except Exception:
            return s
    if re.fullmatch(r"-?\d+\.\d+", s):
        try:
            return float(s)
        except Exception:
            return s
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    return s


def _try_parse_tool_call(text: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    """
    Strictly parse a single tool call from model text.
    Supported forms:
      - tool_name({...json...})
      - tool_name(key=value, key=value)
      - tool_name key=value key=value
      - tool_name(node=2)
      - {"tool":"...", "args":{...}}
    Returns (tool, args) or None.
    """
    if not text:
        return None
    t = text.strip()

    # JSON object form
    if t.startswith("{") and t.endswith("}"):
        try:
            obj = json.loads(t)
            if isinstance(obj, dict) and "tool" in obj and "args" in obj and isinstance(obj["args"], dict):
                return str(obj["tool"]), obj["args"]
        except Exception:
            pass

    # tool_name(...)
    m = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*\((.*)\)\s*$", t)
    if m:
        tool = m.group(1)
        inner = m.group(2).strip()
        if inner == "":
            return tool, {}
        if inner.startswith("{") and inner.endswith("}"):
            try:
                args = json.loads(inner)
                if isinstance(args, dict):
                    return tool, args
            except Exception:
                return None
        args2: Dict[str, Any] = {}
        parts = [p.strip() for p in inner.split(",") if p.strip()]
        for p in parts:
            if "=" not in p:
                return None
            k, v = p.split("=", 1)
            args2[k.strip()] = _parse_value(v.strip())
        return tool, args2

    # tool_name key=value ...
    m2 = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s+(.*)$", t)
    if m2:
        tool = m2.group(1)
        rest = m2.group(2).strip()
        args3: Dict[str, Any] = {}
        for tok in rest.split():
            if "=" not in tok:
                return None
            k, v = tok.split("=", 1)
            args3[k.strip()] = _parse_value(v.strip())
        return tool, args3

    return None


# =============================================================================
# Agent
# =============================================================================

class LightningAgent:
    def __init__(self) -> None:
        repo_root = _repo_root()
        lock_path = repo_root / "runtime" / "agent" / "agent.lock"
        self._lock = StartupLock(lock_path)
        self._lock.acquire_or_exit()

        self.mcp: MCPClient = FastMCPClientWrapper(FastMCPClient())
        self.backend = create_backend()

        self.tick_s = float(_env_int("AGENT_TICK_MS", 500)) / 1000.0

        # LLM controls
        self.allow_llm = _env_bool("ALLOW_LLM", default=False)
        self.min_llm_interval_s = float(_env_int("LLM_MIN_INTERVAL_MS", 1000)) / 1000.0
        self._next_llm_time = _now_monotonic()

        self.max_steps_per_command = _env_int("LLM_MAX_STEPS_PER_COMMAND", 60)
        self.llm_max_output_tokens = _env_int("LLM_MAX_OUTPUT_TOKENS", 900)
        self.llm_temperature = _env_float("LLM_TEMPERATURE", 0.2)

        self.goal_verify_enabled = _env_bool("GOAL_VERIFY", default=True)

        # Trace log path (reset per prompt)
        self.trace = TraceLogger(_runtime_agent_dir() / "trace.log")

    def _log(self, kind: str, payload: Dict[str, Any]) -> None:
        out = {"ts": int(time.time()), "kind": kind, **payload}
        print(json.dumps(out, ensure_ascii=False), flush=True)

    def _sleep_to_next_tick(self, start_t: float) -> None:
        elapsed = _now_monotonic() - start_t
        time.sleep(max(0.0, self.tick_s - elapsed))

    def _write_report(self, req_id: int, content: str, extra: Optional[Dict[str, Any]] = None) -> None:
        entry: Dict[str, Any] = {
            "ts": int(time.time()),
            "type": "agent_report",
            "request_id": req_id,
            "content": content,
        }
        if extra:
            entry.update(extra)
        write_outbox(entry)

    def _llm_allowed(self) -> bool:
        return _now_monotonic() >= self._next_llm_time

    def _reserve_llm(self) -> None:
        self._next_llm_time = max(self._next_llm_time, _now_monotonic() + self.min_llm_interval_s)

    # -------------------------------------------------------------------------
    # Tool schema exposed to the LLM
    # -------------------------------------------------------------------------

    def _llm_tools_full(self) -> List[Dict[str, Any]]:
        return [
            # Health
            {"type": "function", "function": {"name": "network_health", "description": "Check Bitcoin+Lightning health.", "parameters": {"type": "object", "properties": {}}}},

            # Bitcoin
            {"type": "function", "function": {"name": "btc_getblockchaininfo", "description": "Get blockchain status.", "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {"name": "btc_wallet_ensure", "description": "Ensure wallet exists+loaded.", "parameters": {"type": "object", "properties": {"wallet_name": {"type": "string"}}, "required": ["wallet_name"]}}},
            {"type": "function", "function": {"name": "btc_getnewaddress", "description": "Get new address (optional wallet).", "parameters": {"type": "object", "properties": {"wallet": {"type": "string"}}, "required": []}}},
            {"type": "function", "function": {"name": "btc_sendtoaddress", "description": "Send BTC (wallet-aware; default wallet=miner).", "parameters": {"type": "object", "properties": {"address": {"type": "string"}, "amount_btc": {"type": "string"}, "wallet": {"type": "string"}}, "required": ["address", "amount_btc"]}}},
            {"type": "function", "function": {"name": "btc_generatetoaddress", "description": "Mine blocks.", "parameters": {"type": "object", "properties": {"blocks": {"type": "integer"}, "address": {"type": "string"}}, "required": ["blocks", "address"]}}},

            # Node lifecycle
            {"type": "function", "function": {"name": "ln_listnodes", "description": "List node dirs.", "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {"name": "ln_node_status", "description": "Is node running.", "parameters": {"type": "object", "properties": {"node": {"type": "integer"}}, "required": ["node"]}}},
            {"type": "function", "function": {"name": "ln_node_start", "description": "Start node.", "parameters": {"type": "object", "properties": {"node": {"type": "integer"}}, "required": ["node"]}}},

            # Lightning read
            {"type": "function", "function": {"name": "ln_getinfo", "description": "Get node info.", "parameters": {"type": "object", "properties": {"node": {"type": "integer"}}, "required": ["node"]}}},
            {"type": "function", "function": {"name": "ln_listpeers", "description": "List peers for node.", "parameters": {"type": "object", "properties": {"node": {"type": "integer"}}, "required": ["node"]}}},
            {"type": "function", "function": {"name": "ln_listfunds", "description": "List onchain outputs and channels.", "parameters": {"type": "object", "properties": {"node": {"type": "integer"}}, "required": ["node"]}}},
            {"type": "function", "function": {"name": "ln_listchannels", "description": "List peer channels (mapped to listpeerchannels).", "parameters": {"type": "object", "properties": {"node": {"type": "integer"}}, "required": ["node"]}}},
            {"type": "function", "function": {"name": "ln_newaddr", "description": "Get new on-chain address for node wallet.", "parameters": {"type": "object", "properties": {"node": {"type": "integer"}}, "required": ["node"]}}},

            # Lightning actions
            {"type": "function", "function": {"name": "ln_connect", "description": "Connect peer by id/host/port.", "parameters": {"type": "object", "properties": {"from_node": {"type": "integer"}, "peer_id": {"type": "string"}, "host": {"type": "string"}, "port": {"type": "integer"}}, "required": ["from_node", "peer_id", "host", "port"]}}},
            {"type": "function", "function": {"name": "ln_openchannel", "description": "Open channel.", "parameters": {"type": "object", "properties": {"from_node": {"type": "integer"}, "peer_id": {"type": "string"}, "amount_sat": {"type": "integer"}}, "required": ["from_node", "peer_id", "amount_sat"]}}},

            # Payments
            {"type": "function", "function": {"name": "ln_invoice", "description": "Create invoice (returns payload.bolt11).", "parameters": {"type": "object", "properties": {"node": {"type": "integer"}, "amount_msat": {"type": "integer"}, "label": {"type": "string"}, "description": {"type": "string"}}, "required": ["node", "amount_msat", "label", "description"]}}},
            {"type": "function", "function": {"name": "ln_pay", "description": "Pay BOLT11 invoice.", "parameters": {"type": "object", "properties": {"from_node": {"type": "integer"}, "bolt11": {"type": "string"}}, "required": ["from_node", "bolt11"]}}},
        ]

    # -------------------------------------------------------------------------
    # Centralized MCP call with normalization + trace
    # -------------------------------------------------------------------------

    def _call_tool_traced(self, req_id: int, name: str, args: Any, source: str, tool_calls_made: List[str]) -> Dict[str, Any]:
        norm_args, norm_err, changed = _normalize_tool_args(name, args)

        if changed:
            self.trace.log({"event": "tool_args_normalized", "tool": name, "source": source, "before": args, "after": norm_args})

        if norm_err:
            self.trace.log({"event": "tool_args_invalid", "tool": name, "source": source, "error": norm_err, "args": norm_args})
            # Return in the same shape as MCP error path so caller can fail fast deterministically.
            return {"id": 0, "error": norm_err}

        sig = _tool_sig(name, norm_args)
        tool_calls_made.append(name)
        self.trace.log({"event": "tool_call", "source": source, "tool": name, "args": norm_args, "sig": sig})

        result = self.mcp.call(name, args=norm_args)
        err = _is_tool_error(result)

        self.trace.log(
            {
                "event": "tool_result",
                "source": source,
                "tool": name,
                "sig": sig,
                "ok": err is None,
                "error": err,
                "result_summary": _summarize_tool_result(result),
                "raw_result": result,
            }
        )
        return result

    # -------------------------------------------------------------------------
    # Goal verification (controller behavior)
    # -------------------------------------------------------------------------

    def _verify_payment_readiness(self, req_id: int, tool_calls_made: List[str]) -> Tuple[bool, str, Dict[str, Any]]:
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
        peer_count = len(p_payload.get("peers", [])) if isinstance(p_payload, dict) and isinstance(p_payload.get("peers"), list) else 0
        if peer_count < 1:
            return False, "No peers connected from node-1 (need at least 1 peer).", details

        funds = self._call_tool_traced(req_id, "ln_listfunds", {"node": 1}, "verify", tool_calls_made)
        details["listfunds_1"] = funds
        f_payload = _safe_get(funds, "result", "payload", default={})
        ch_count = len(f_payload.get("channels", [])) if isinstance(f_payload, dict) and isinstance(f_payload.get("channels"), list) else 0
        if ch_count < 1:
            return False, "No channels found on node-1 (need at least 1 channel).", details

        return True, "Ready to attempt ln_invoice + ln_pay.", details

    # -------------------------------------------------------------------------
    # LLM orchestration
    # -------------------------------------------------------------------------

    def _handle_freeform_llm(self, req_id: int, user_text: str) -> None:
        self.trace.reset(
            {
                "ts": int(time.time()),
                "event": "prompt_start",
                "build": AGENT_BUILD,
                "request_id": req_id,
                "allow_llm": self.allow_llm,
                "max_steps_per_command": self.max_steps_per_command,
                "user_text": user_text,
            }
        )

        if not self.allow_llm:
            self.trace.log({"event": "llm_disabled"})
            self._write_report(req_id, "LLM is disabled (ALLOW_LLM!=1).")
            return

        tools = self._llm_tools_full()
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

        seen_since_state_change: set[str] = set()
        consecutive_read_only = 0
        MAX_CONSEC_READ_ONLY = 8
        recent_sigs: Deque[str] = deque(maxlen=12)

        require_tool_next = False
        refused_tool_count = 0
        MAX_REFUSED_TOOL = 1  # after 1 refusal, we try fallback; after that, stop

        steps = 0
        while steps < self.max_steps_per_command:
            steps += 1
            if not self._llm_allowed():
                time.sleep(0.05)
                continue
            self._reserve_llm()

            self.trace.log({"event": "llm_step_begin", "llm_step": steps, "require_tool_next": require_tool_next})

            req = LLMRequest(messages=messages, tools=tools, max_output_tokens=self.llm_max_output_tokens, temperature=self.llm_temperature)
            resp = self.backend.step(req)

            self.trace.log(
                {
                    "event": "llm_step_response",
                    "llm_step": steps,
                    "resp_type": getattr(resp, "type", None),
                    "content_preview": (getattr(resp, "content", "") or "")[:400],
                    "reasoning_preview": (getattr(resp, "reasoning", "") or "")[:400],
                }
            )

            if resp.type == "tool_call":
                require_tool_next = False
                refused_tool_count = 0

                for tc in resp.tool_calls:
                    name = tc.name
                    args = tc.args or {}

                    # Normalize + validate BEFORE sig/repeat gating (so wrapped args normalize deterministically)
                    norm_args, norm_err, changed = _normalize_tool_args(name, args)
                    if changed:
                        self.trace.log({"event": "tool_args_normalized", "tool": name, "source": "llm", "before": args, "after": norm_args})
                    if norm_err:
                        msg = f"STOPPED: invalid tool args for {name}: {norm_err}"
                        self.trace.log({"event": "stop", "reason": "tool_args_invalid", "message": msg, "tool": name, "args": norm_args})
                        self._write_report(req_id, msg, extra={"used_llm": True, "steps": steps, "tool_calls": tool_calls_made})
                        return
                    args = norm_args

                    sig = _tool_sig(name, args)
                    self.trace.log({"event": "tool_requested", "llm_step": steps, "tool": name, "args": args, "sig": sig})

                    if name in READ_ONLY_TOOLS and sig in seen_since_state_change:
                        msg = f"STOPPED: blocked redundant tool recall (no state change): {name} args={args}"
                        self.trace.log({"event": "stop", "reason": "redundant_recall", "message": msg, "sig": sig})
                        self._write_report(req_id, msg, extra={"used_llm": True, "steps": steps, "tool_calls": tool_calls_made, "blocked_sig": sig})
                        return

                    tool_calls_made.append(name)
                    recent_sigs.append(sig)

                    if len(recent_sigs) >= 8:
                        window = list(recent_sigs)[-8:]
                        uniq = set(window)
                        if len(uniq) <= 2 and all(s.split(":", 1)[0] in READ_ONLY_TOOLS for s in uniq):
                            msg = "STOPPED: tool-call oscillation detected (<=2 unique read-only signatures repeating)."
                            self.trace.log({"event": "stop", "reason": "oscillation", "message": msg, "recent_sigs": list(recent_sigs)})
                            self._write_report(req_id, msg, extra={"used_llm": True, "steps": steps, "tool_calls": tool_calls_made, "recent_sigs": list(recent_sigs)})
                            return

                    # Call MCP (direct) with already-normalized args, but keep trace structure consistent
                    result = self.mcp.call(name, args=args)
                    err = _is_tool_error(result)

                    self.trace.log(
                        {
                            "event": "tool_result",
                            "llm_step": steps,
                            "tool": name,
                            "sig": sig,
                            "ok": err is None,
                            "error": err,
                            "result_summary": _summarize_tool_result(result),
                            "raw_result": result,
                        }
                    )

                    messages.append({"role": "assistant", "content": resp.reasoning or ""})
                    messages.append({"role": "tool", "name": name, "content": json.dumps(result, ensure_ascii=False)})

                    if err:
                        msg = f"STOPPED: tool error in {name}\nError: {err}"
                        self.trace.log({"event": "stop", "reason": "tool_error", "message": msg})
                        self._write_report(req_id, f"{msg}\n\nRaw tool result:\n{json.dumps(result, indent=2, ensure_ascii=False)}", extra={"used_llm": True, "steps": steps, "tool_calls": tool_calls_made})
                        return

                    seen_since_state_change.add(sig)

                    if name in READ_ONLY_TOOLS:
                        consecutive_read_only += 1
                        if consecutive_read_only > MAX_CONSEC_READ_ONLY:
                            msg = "STOPPED: too many read-only tool calls without a state-changing action (stuck observing)."
                            self.trace.log({"event": "stop", "reason": "too_many_read_only", "message": msg, "recent_sigs": list(recent_sigs)})
                            self._write_report(req_id, msg, extra={"used_llm": True, "steps": steps, "tool_calls": tool_calls_made, "recent_sigs": list(recent_sigs)})
                            return
                    else:
                        consecutive_read_only = 0

                    if name in STATE_CHANGING_TOOLS:
                        seen_since_state_change.clear()
                        consecutive_read_only = 0
                        self.trace.log({"event": "state_changed", "tool": name, "note": "cleared recall gate + reset read-only counter"})

                continue

            # FINAL branch
            final_text = resp.content or ""
            self.trace.log({"event": "final_candidate", "llm_step": steps, "text_preview": final_text[:800]})

            if require_tool_next:
                parsed = _try_parse_tool_call(final_text)
                self.trace.log({"event": "fallback_parse_attempt", "llm_step": steps, "text": final_text, "parsed": parsed})
                if parsed:
                    tname, targs = parsed
                    if tname in FALLBACK_ALLOWED_TOOLS:
                        # normalize parsed args too
                        norm_args, norm_err, changed = _normalize_tool_args(tname, targs)
                        if changed:
                            self.trace.log({"event": "tool_args_normalized", "tool": tname, "source": "fallback", "before": targs, "after": norm_args})
                        if norm_err:
                            msg = f"STOPPED: invalid fallback tool args for {tname}: {norm_err}"
                            self.trace.log({"event": "stop", "reason": "tool_args_invalid", "message": msg, "tool": tname, "args": norm_args})
                            self._write_report(req_id, msg, extra={"used_llm": True, "steps": steps, "tool_calls": tool_calls_made})
                            return

                        sig = _tool_sig(tname, norm_args)
                        self.trace.log({"event": "fallback_tool_execute", "tool": tname, "args": norm_args, "sig": sig})
                        tool_calls_made.append(tname)

                        result = self.mcp.call(tname, args=norm_args)
                        err = _is_tool_error(result)
                        self.trace.log({"event": "fallback_tool_result", "tool": tname, "ok": err is None, "error": err, "raw_result": result})
                        if err:
                            msg = f"STOPPED: tool error in {tname}\nError: {err}"
                            self._write_report(req_id, f"{msg}\n\nRaw tool result:\n{json.dumps(result, indent=2, ensure_ascii=False)}", extra={"used_llm": True, "steps": steps, "tool_calls": tool_calls_made})
                            return
                        continue

                refused_tool_count += 1
                self.trace.log({"event": "refused_tools", "count": refused_tool_count})
                if refused_tool_count > MAX_REFUSED_TOOL:
                    msg = "STOPPED: LLM refused to call tools while goal unmet."
                    self.trace.log({"event": "stop", "reason": "llm_refused_tools", "message": msg})
                    self._write_report(req_id, msg, extra={"used_llm": True, "steps": steps, "tool_calls": tool_calls_made})
                    return

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
                    messages.append({"role": "assistant", "content": final_text})
                    messages.append({"role": "system", "content": fix_msg})
                    self.trace.log({"event": "forced_tools_injected", "text": fix_msg})
                    continue

            self.trace.log({"event": "prompt_done", "llm_step": steps})
            self._write_report(req_id, final_text, extra={"used_llm": True, "steps": steps, "tool_calls": tool_calls_made})
            return

        msg = "ERROR: exceeded max steps."
        self.trace.log({"event": "stop", "reason": "max_steps", "message": msg, "tool_calls": tool_calls_made})
        self._write_report(req_id, msg, extra={"used_llm": True, "steps": steps, "tool_calls": tool_calls_made})

    # -------------------------------------------------------------------------
    # Main loop
    # -------------------------------------------------------------------------

    def run(self) -> None:
        self._log("agent_start", {"msg": "Agent online (single instance). Waiting for inbox commands.", "build": AGENT_BUILD})

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