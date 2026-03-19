from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple


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
# Tool required-arg registry
# =============================================================================

TOOL_REQUIRED: Dict[str, List[str]] = {
    # Health
    "network_health": [],

    # Bitcoin
    "btc_getblockchaininfo": [],
    "btc_wallet_ensure": ["wallet_name"],
    "btc_getnewaddress": [],
    "btc_sendtoaddress": ["address", "amount_btc"],
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


# =============================================================================
# Arg normalization
# =============================================================================

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
# Error detection
# =============================================================================

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


# =============================================================================
# Utilities
# =============================================================================

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
      - {"tool":"...", "args":{...}}
    Returns (tool, args) or None.
    """
    if not text:
        return None
    t = text.strip()

    if t.startswith("{") and t.endswith("}"):
        try:
            obj = json.loads(t)
            if isinstance(obj, dict) and "tool" in obj and "args" in obj and isinstance(obj["args"], dict):
                return str(obj["tool"]), obj["args"]
        except Exception:
            pass

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
    """Return the full MCP tool schema list in OpenAI function-calling format."""
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
        {"type": "function", "function": {"name": "ln_pay", "description": "Pay BOLT11 invoice.", "parameters": {"type": "object", "properties": {"from_node": {"type": "integer"}, "bolt11": {"type": "string"}}, "required": ["from_node", "bolt11"]}}},
    ]


def llm_tools_schema_text() -> str:
    """Return tool schema as human-readable text (for embedding in planner system prompt)."""
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
