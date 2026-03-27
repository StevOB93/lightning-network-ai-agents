from __future__ import annotations

# =============================================================================
# ai.tools — MCP tool registry, normalization, and schema generation
#
# Central reference for everything tool-related. Imported by:
#   - ai.agent (legacy agent mode)
#   - ai.pipeline (pipeline coordinator)
#   - ai.controllers.executor (step execution)
#   - ai.controllers.planner (system prompt generation)
#   - ai.controllers.translator (indirectly via _normalize_tool_args)
#
# Contents:
#   Tool category sets    — READ_ONLY_TOOLS, STATE_CHANGING_TOOLS, etc.
#   TOOL_REQUIRED         — required arg names per tool (for validation)
#   _normalize_tool_args  — unwrap, coerce, range-validate, required-check
#   _is_tool_error        — extract error string from nested MCP result shapes
#   _tool_sig             — deterministic fingerprint for oscillation detection
#   _summarize_tool_result— compact one-line summary for trace logs
#   _try_parse_tool_call  — fallback text parser for non-structured tool calls
#   llm_tools_schema      — full OpenAI function-calling schema list
#   llm_tools_schema_text — human-readable tool list for system prompts
# =============================================================================

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# =============================================================================
# Tool enforcement policy — category sets
# =============================================================================

# Tools that observe but never mutate state.
# Used by the agent's oscillation detector and redundant-recall gate:
#   - A repeated call with the same args since the last state change is blocked.
#   - More than MAX_CONSEC_READ_ONLY consecutive calls triggers a "stuck" stop.
READ_ONLY_TOOLS = {
    "network_health",
    "memory_lookup",        # Read-only: queries the episodic archive, no side effects
    "sys_netinfo",          # Read-only: queries the OS routing table, no side effects
    "btc_getblockchaininfo",
    "btc_getnewaddress",    # Returns an address but doesn't spend or mutate channels
    "ln_listnodes",
    "ln_node_status",
    "ln_getinfo",
    "ln_listpeers",
    "ln_listfunds",
    "ln_listchannels",
    "ln_newaddr",
    "ln_listinvoices",
    "ln_waitinvoice",
}

# Tools that change node/channel/wallet state.
# When one of these succeeds, the agent's recall gate is cleared (seen_since_state_change.clear())
# and the read-only counter is reset — the LLM is allowed to re-read state that may have changed.
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

# Union of both categories: tools that the fallback text parser is allowed to execute.
# This ensures a fallback-parsed tool name isn't something outside our known set.
FALLBACK_ALLOWED_TOOLS = READ_ONLY_TOOLS | STATE_CHANGING_TOOLS


# =============================================================================
# Tool required-arg registry
# =============================================================================

# Maps each tool name to its list of required argument keys.
# Used by _normalize_tool_args for two purposes:
#   1. Detect if required keys are missing so we can try unwrapping {"args": {...}}
#   2. Final validation: return an error if keys are still missing after unwrapping
TOOL_REQUIRED: Dict[str, List[str]] = {
    # Health / system info
    "network_health":  [],
    "memory_lookup":   [],  # all args optional: query, last_n, outcome
    "sys_netinfo":     [],

    # Bitcoin
    "btc_getblockchaininfo": [],
    "btc_wallet_ensure": ["wallet_name"],
    "btc_getnewaddress": [],
    "btc_sendtoaddress": ["address", "amount_btc"],  # wallet is optional
    "btc_generatetoaddress": ["blocks", "address"],

    # Node lifecycle
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
    "ln_listinvoices": ["node"],
    "ln_waitinvoice": ["node", "label"],
}

# Fields that must be integers — LLMs sometimes emit them as strings ("1" instead of 1)
_INT_KEYS = {"node", "from_node", "port", "blocks", "amount_sat", "amount_msat"}

# Subset of _INT_KEYS that represent node numbers — validated against node_count
_NODE_KEYS = {"node", "from_node"}


def _get_node_count() -> int:
    """
    Read the active node count from runtime/node_count.

    Written by the launcher when nodes start. Falls back to 2 (standard regtest
    pair) if the file is absent or unreadable. Read on every normalization call
    so changes take effect without restarting the agent.
    """
    try:
        p = Path(__file__).resolve().parent.parent / "runtime" / "node_count"
        return int(p.read_text().strip())
    except (FileNotFoundError, ValueError):
        fallback = os.getenv("DEFAULT_NODE_COUNT")
        try:
            return int(fallback) if fallback else 2
        except (ValueError, TypeError):
            return 2


# =============================================================================
# Arg normalization
# =============================================================================

def _coerce_int_fields(args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert string values to int for known integer fields.

    LLMs sometimes emit {"node": "1"} instead of {"node": 1} — this fix
    ensures downstream code that does integer arithmetic or comparisons works
    correctly. Only converts strings that are purely numeric (via .isdigit()).
    Negative values and floats are intentionally left alone.
    """
    out = dict(args)
    for k, v in list(out.items()):
        if k in _INT_KEYS and isinstance(v, str):
            vs = v.strip()
            if vs.isdigit():
                out[k] = int(vs)
    return out


def _normalize_tool_args(tool: str, args: Any) -> Tuple[Dict[str, Any], Optional[str], bool]:
    """
    Normalize, validate, and return tool arguments.

    Returns (normalized_args, error_or_none, changed_bool).

    Processing steps (in order):
      1. Coerce args to dict if it's not already one
      2. If required keys are missing and there's an inner {"args": {...}} dict,
         unwrap it (LLMs sometimes wrap args in a nested "args" key)
      3. Coerce string integer fields to int
      4. Validate node numbers are within [1, node_count]
      5. Validate bitcoin addresses have a valid regtest prefix
      6. Check all required keys are present

    The `changed` bool indicates whether any normalization was applied —
    the caller uses this to decide whether to log a normalization event.

    Error strings describe the first validation failure found. The executor
    treats a non-None error as a hard failure for that step.
    """
    changed = False

    a: Dict[str, Any] = args if isinstance(args, dict) else {}
    reqs = TOOL_REQUIRED.get(tool)

    # Step 2: Unwrap nested {"args": {...}} if required keys are missing
    if reqs is not None and reqs:
        missing = [k for k in reqs if k not in a]
        if missing:
            inner = a.get("args")
            if isinstance(inner, dict):
                merged = dict(a)
                merged.pop("args", None)
                merged.update(inner)  # Inner args win over outer
                a = merged
                changed = True

    # Step 3: Coerce string integers
    a2 = _coerce_int_fields(a)
    if a2 != a:
        changed = True
    a = a2

    # Step 4: Validate node number ranges
    node_count = _get_node_count()
    for key in _NODE_KEYS:
        if key in a and isinstance(a[key], int):
            if a[key] < 1 or a[key] > node_count:
                return a, f"node {a[key]} out of range (valid: 1-{node_count})", changed

    # Step 4b: Validate amount ranges (must be positive integers/floats)
    if "amount_msat" in a and isinstance(a["amount_msat"], int) and a["amount_msat"] <= 0:
        return a, "amount_msat must be a positive integer (millisatoshis)", changed
    if "amount_sat" in a and isinstance(a["amount_sat"], int) and a["amount_sat"] <= 0:
        return a, "amount_sat must be a positive integer (satoshis)", changed
    if "amount_btc" in a:
        try:
            btc_val = float(a["amount_btc"])
        except (TypeError, ValueError):
            return a, f"amount_btc is not a valid number: {a['amount_btc']!r}", changed
        if btc_val <= 0:
            return a, "amount_btc must be a positive number", changed

    # Step 4c: Validate port range
    if "port" in a and isinstance(a["port"], int):
        if a["port"] < 1 or a["port"] > 65535:
            return a, f"port {a['port']} out of range (valid: 1-65535)", changed

    # Step 4d: Validate bolt11 basic format (Lightning invoice prefix)
    if "bolt11" in a and isinstance(a["bolt11"], str):
        b11 = a["bolt11"].lower()
        if not (b11.startswith("lnbcrt") or b11.startswith("lnbc") or b11.startswith("lntb")):
            return a, f"bolt11 does not look like a Lightning invoice: {a['bolt11'][:30]}...", changed

    # Step 5: Validate bitcoin addresses for regtest network
    # Valid regtest prefixes: bcrt1 (segwit), 2 (P2SH), m/n (legacy)
    if tool in ("btc_generatetoaddress", "btc_sendtoaddress"):
        addr = a.get("address", "")
        if isinstance(addr, str) and addr:
            if not addr.startswith(("bcrt1", "2", "m", "n")):
                return a, f"invalid regtest address: {addr[:20]}...", changed

    # Step 6: Final required-key check (after all normalization attempts)
    if reqs is not None and reqs:
        missing2 = [k for k in reqs if k not in a]
        if missing2:
            return a, f"tool args missing required keys: {missing2}", changed

    return a, None, changed


# =============================================================================
# Error detection
# =============================================================================

def _is_tool_error(result: Any) -> Optional[str]:
    """
    Extract an error message from an MCP tool result, or return None if OK.

    MCP tools use three different error shapes; this handles all of them:

      Shape 1 (top-level error):
        {"error": "message string"}

      Shape 2 (top-level ok=false):
        {"ok": false, "error": "message string"}

      Shape 3 (nested result wrapper):
        {"result": {"ok": false, "error": "message string"}}
        {"result": {"error": "message string"}}

    Returns the error string if any shape indicates failure, None if OK.
    """
    if not isinstance(result, dict):
        return None

    # Shape 1: top-level error field
    if "error" in result and isinstance(result["error"], str) and result["error"].strip():
        return result["error"].strip()

    # Shape 2: top-level ok=false
    if result.get("ok") is False:
        err = result.get("error")
        return str(err) if err else "Tool returned ok=false"

    # Shape 3: nested result dict
    inner = result.get("result")
    if isinstance(inner, dict):
        if "error" in inner and isinstance(inner["error"], str) and inner["error"].strip():
            return inner["error"].strip()
        if inner.get("ok") is False:
            err = inner.get("error")
            return str(err) if err else "Tool returned ok=false"

    return None


# =============================================================================
# Utilities
# =============================================================================

def _tool_sig(name: str, args: Dict[str, Any]) -> str:
    """
    Produce a deterministic string fingerprint for a tool call.

    Format: "tool_name:{"key": value, ...}" (keys sorted for stability).
    Used by the agent's oscillation detector and redundant-recall gate to
    compare tool calls across steps without maintaining separate data structures.
    Falls back to str() representation if the args are not JSON-serializable.
    """
    try:
        return f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"
    except Exception:
        return f"{name}:{str(args)}"


def _summarize_tool_result(result: Any, max_len: int = 400) -> str:
    """
    Produce a compact one-line summary of a tool result for trace logging.

    Handles the three MCP result shapes:
      error field              → "error=<message>"
      nested ok=false          → "ok=false error=<message>"
      nested ok=true + payload → "ok=true payload=<JSON>"
      other                    → truncated JSON

    max_len caps the output length so trace log entries stay readable.
    """
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
# Fallback tool-call text parser
# =============================================================================

def _parse_value(s: str) -> Any:
    """
    Parse a single string token to its Python type.

    Used by the kwargs-form and space-form parsers in _try_parse_tool_call.
    Converts: "true"/"false" → bool, "null"/"none" → None,
              integer strings → int, float strings → float,
              quoted strings → unquoted string, other → raw string.
    """
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
    Parse a tool call from plain text when the LLM used non-structured format.

    Called as a fallback in the agent loop when the LLM returns a "final"
    response while require_tool_next is True — the LLM may have embedded the
    tool call as text rather than using the structured tool_calls format.

    Supported input forms (tried in order):
      1. {"tool":"name", "args":{...}}          JSON object form
      2. tool_name({...json...})                Function + JSON body
      3. tool_name(key=value, key=value)        Function + kwargs
      4. tool_name key=value key=value          Space-separated kwargs

    Returns (tool_name, args_dict) on success, None if no form matches.
    The caller is responsible for validating that the tool name is in
    FALLBACK_ALLOWED_TOOLS before executing.
    """
    if not text:
        return None
    t = text.strip()

    # Form 1: JSON object with "tool" and "args" keys
    if t.startswith("{") and t.endswith("}"):
        try:
            obj = json.loads(t)
            if isinstance(obj, dict) and "tool" in obj and "args" in obj and isinstance(obj["args"], dict):
                return str(obj["tool"]), obj["args"]
        except Exception:
            pass

    # Forms 2 + 3: tool_name(...) — either JSON body or key=value pairs
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
                return None  # Malformed JSON inside parens; don't proceed
        # Form 3: key=value, key=value
        args2: Dict[str, Any] = {}
        parts = [p.strip() for p in inner.split(",") if p.strip()]
        for p in parts:
            if "=" not in p:
                return None
            k, v = p.split("=", 1)
            args2[k.strip()] = _parse_value(v.strip())
        return tool, args2

    # Form 4: tool_name key=value key2=value2
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
# Tool schema (for LLM context)
# =============================================================================

def llm_tools_schema() -> List[Dict[str, Any]]:
    """
    Return the full MCP tool schema in OpenAI function-calling format.

    This is the definitive list of tools exposed to the LLM. Used by:
      - The agent's step() call (passed as the `tools` parameter)
      - The planner's system prompt (via llm_tools_schema_text())
      - _allowed_tool_names() in the Ollama fallback parser

    Format: [{"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}}}]
    """
    return [
        # Health / system info
        {"type": "function", "function": {"name": "network_health", "description": "Check Bitcoin+Lightning health.", "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {"name": "memory_lookup", "description": "Query the episodic archive of past pipeline runs. Returns previous prompts, goals, outcomes, and summaries. Use for recall intents: 'what did I run last time?', 'did the payment succeed?', 'show recent history'. To find specific topics, use the 'query' keyword filter (e.g. query='payment'). The 'outcome' field only accepts ok/partial/failed — do NOT use it for topic filtering.", "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "Keyword to filter by (matched against user prompt and goal text). Use this for topic searches like 'payment', 'channel', 'invoice'. Omit to return all recent entries."}, "last_n": {"type": "integer", "description": "Number of most recent matches to return (default 5)."}, "outcome": {"type": "string", "enum": ["ok", "partial", "failed"], "description": "Filter by execution outcome ONLY. Must be exactly one of: ok, partial, failed. Omit to include all outcomes."}}, "required": []}}},
        {"type": "function", "function": {"name": "sys_netinfo", "description": "Get this machine's hostname and non-loopback IPs. Use before ln_node_start(bind_host, announce_host) to find the correct IP for cross-machine Lightning peer connectivity.", "parameters": {"type": "object", "properties": {}}}},

        # Bitcoin
        {"type": "function", "function": {"name": "btc_getblockchaininfo", "description": "Get blockchain status.", "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {"name": "btc_wallet_ensure", "description": "Ensure wallet exists+loaded.", "parameters": {"type": "object", "properties": {"wallet_name": {"type": "string"}}, "required": ["wallet_name"]}}},
        {"type": "function", "function": {"name": "btc_getnewaddress", "description": "Get new address (optional wallet).", "parameters": {"type": "object", "properties": {"wallet": {"type": "string"}}, "required": []}}},
        {"type": "function", "function": {"name": "btc_sendtoaddress", "description": "Send BTC (wallet-aware; default wallet=miner).", "parameters": {"type": "object", "properties": {"address": {"type": "string"}, "amount_btc": {"type": "string"}, "wallet": {"type": "string"}}, "required": ["address", "amount_btc"]}}},
        {"type": "function", "function": {"name": "btc_generatetoaddress", "description": "Mine blocks.", "parameters": {"type": "object", "properties": {"blocks": {"type": "integer"}, "address": {"type": "string"}}, "required": ["blocks", "address"]}}},

        # Node lifecycle
        {"type": "function", "function": {"name": "ln_listnodes", "description": "List node dirs.", "parameters": {"type": "object", "properties": {}}}},
        {"type": "function", "function": {"name": "ln_node_status", "description": "Is node running.", "parameters": {"type": "object", "properties": {"node": {"type": "integer"}}, "required": ["node"]}}},
        {"type": "function", "function": {"name": "ln_node_start", "description": "Start node. Optional: bind_host (IP to bind on, e.g. '0.0.0.0' for all interfaces) and announce_host (IP to advertise to peers) for cross-machine connectivity. Call sys_netinfo first to get the correct announce_host.", "parameters": {"type": "object", "properties": {"node": {"type": "integer"}, "bind_host": {"type": "string", "description": "IP to bind on. Omit for default (127.0.0.1)."}, "announce_host": {"type": "string", "description": "IP/hostname to advertise to peers. Omit for default."}}, "required": ["node"]}}},
        {"type": "function", "function": {"name": "ln_node_create", "description": "Create node dir.", "parameters": {"type": "object", "properties": {"node": {"type": "integer"}}, "required": ["node"]}}},
        {"type": "function", "function": {"name": "ln_node_stop", "description": "Stop node.", "parameters": {"type": "object", "properties": {"node": {"type": "integer"}}, "required": ["node"]}}},
        {"type": "function", "function": {"name": "ln_node_delete", "description": "Delete node dir.", "parameters": {"type": "object", "properties": {"node": {"type": "integer"}}, "required": ["node"]}}},

        # Lightning read
        {"type": "function", "function": {"name": "ln_getinfo", "description": "Get node info.", "parameters": {"type": "object", "properties": {"node": {"type": "integer"}}, "required": ["node"]}}},
        {"type": "function", "function": {"name": "ln_listpeers", "description": "List peers for node.", "parameters": {"type": "object", "properties": {"node": {"type": "integer"}}, "required": ["node"]}}},
        {"type": "function", "function": {"name": "ln_listfunds", "description": "List onchain outputs and channels.", "parameters": {"type": "object", "properties": {"node": {"type": "integer"}}, "required": ["node"]}}},
        {"type": "function", "function": {"name": "ln_listchannels", "description": "List peer channels.", "parameters": {"type": "object", "properties": {"node": {"type": "integer"}}, "required": ["node"]}}},
        {"type": "function", "function": {"name": "ln_newaddr", "description": "Get new on-chain address for node wallet.", "parameters": {"type": "object", "properties": {"node": {"type": "integer"}}, "required": ["node"]}}},

        # Lightning actions
        {"type": "function", "function": {"name": "ln_connect", "description": "Connect peer by id/host/port.", "parameters": {"type": "object", "properties": {"from_node": {"type": "integer"}, "peer_id": {"type": "string"}, "host": {"type": "string"}, "port": {"type": "integer"}}, "required": ["from_node", "peer_id", "host", "port"]}}},
        {"type": "function", "function": {"name": "ln_openchannel", "description": "Open channel.", "parameters": {"type": "object", "properties": {"from_node": {"type": "integer"}, "peer_id": {"type": "string"}, "amount_sat": {"type": "integer"}}, "required": ["from_node", "peer_id", "amount_sat"]}}},

        # Payments
        {"type": "function", "function": {"name": "ln_invoice", "description": "Create invoice (returns payload.bolt11).", "parameters": {"type": "object", "properties": {"node": {"type": "integer"}, "amount_msat": {"type": "integer"}, "label": {"type": "string"}, "description": {"type": "string"}}, "required": ["node", "amount_msat", "label", "description"]}}},
        {"type": "function", "function": {"name": "ln_pay", "description": "Pay BOLT11 invoice. Optional: maxfee (max fee in msat), retry_for (seconds to keep retrying).", "parameters": {"type": "object", "properties": {"from_node": {"type": "integer"}, "bolt11": {"type": "string"}, "maxfee": {"type": "integer", "description": "Maximum fee to pay in millisatoshis (optional)"}, "retry_for": {"type": "integer", "description": "Seconds to keep retrying payment (optional, default: CLN default)"}}, "required": ["from_node", "bolt11"]}}},
        {"type": "function", "function": {"name": "ln_listinvoices", "description": "List invoices on a node. Optional: label (filter by specific label).", "parameters": {"type": "object", "properties": {"node": {"type": "integer"}, "label": {"type": "string", "description": "Invoice label to filter by (optional)"}}, "required": ["node"]}}},
        {"type": "function", "function": {"name": "ln_waitinvoice", "description": "Block until a specific invoice is paid. Returns payment details including preimage.", "parameters": {"type": "object", "properties": {"node": {"type": "integer"}, "label": {"type": "string"}}, "required": ["node", "label"]}}},
    ]


def llm_tools_schema_text() -> str:
    """
    Return a human-readable text representation of all MCP tools.

    Used by the Planner's system prompt (embedded as a reference table) so the
    LLM knows the exact tool names, descriptions, and required args when
    generating an ExecutionPlan without the overhead of the full JSON schema.

    Format:
      tool: <name>
        description: <description>
        args:
          <arg>: <type> (required|optional)
    """
    lines = ["Available MCP tools:\n"]
    for entry in llm_tools_schema():
        fn = entry["function"]
        name = fn["name"]
        desc = fn["description"]
        params = fn.get("parameters", {})
        required = params.get("required", [])
        properties = params.get("properties", {})

        args_parts = []
        for arg, schema in properties.items():
            t = schema.get("type", "any")
            req = " (required)" if arg in required else " (optional)"
            args_parts.append(f"    {arg}: {t}{req}")

        lines.append(f"tool: {name}")
        lines.append(f"  description: {desc}")
        if args_parts:
            lines.append("  args:")
            lines.extend(args_parts)
        else:
            lines.append("  args: none")
        lines.append("")

    return "\n".join(lines)
