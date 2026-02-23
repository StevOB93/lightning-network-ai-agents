from __future__ import annotations

import atexit
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ai.command_queue import read_new, write_outbox
from ai.llm.factory import create_backend
from mcp.client.fastmcp import FastMCPClient

try:
    import fcntl  # Linux/WSL
except Exception:
    fcntl = None  # type: ignore


# =============================================================================
# Startup lock (single agent instance)
# =============================================================================

class StartupLock:
    """
    Prevent multiple agent instances from consuming the same inbox.
    Uses fcntl advisory lock on Linux/WSL. The lock is released automatically
    when the process exits (even if it crashes), as long as we keep the fd open.
    """

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path
        self._fh = None  # file handle held for lifetime

    def acquire_or_exit(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

        fh = self.lock_path.open("a+", encoding="utf-8")
        try:
            if fcntl is None:
                # Best-effort fallback: not ideal, but prevents accidental duplicates in most cases.
                # If you ever run on a platform without fcntl, prefer a real lock service.
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

            # Write our PID into the lock file (informational)
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
# Helpers
# =============================================================================

def _now_monotonic() -> float:
    return time.monotonic()


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _short_id(s: str, n: int = 12) -> str:
    if not s:
        return "unknown"
    return s[:n]


def _safe_get(d: Any, *keys: str, default: Any = None) -> Any:
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _extract_binding_host_port(getinfo_payload: Dict[str, Any]) -> Tuple[str, int]:
    binding = getinfo_payload.get("binding", [])
    if isinstance(binding, list):
        for b in binding:
            if isinstance(b, dict) and "address" in b and "port" in b:
                return str(b["address"]), int(b["port"])
    return "127.0.0.1", 9735


def _collect_node_warnings(getinfo_payload: Dict[str, Any]) -> List[str]:
    warns: List[str] = []
    for k, v in getinfo_payload.items():
        if isinstance(k, str) and k.startswith("warning_"):
            warns.append(f"{k}: {v}")
    return warns


def _fmt_table(rows: List[List[str]], headers: List[str]) -> str:
    cols = len(headers)
    widths = [len(h) for h in headers]
    for r in rows:
        for i in range(cols):
            widths[i] = max(widths[i], len(r[i]))

    def fmt_row(r: List[str]) -> str:
        return "  ".join(r[i].ljust(widths[i]) for i in range(cols))

    out = [fmt_row(headers), "-" * len(fmt_row(headers))]
    out.extend(fmt_row(r) for r in rows)
    return "\n".join(out)


def _is_tool_error(result: Any) -> Optional[str]:
    """
    Stop-on-error normalization across common tool return shapes.
    Treats these as errors:
      - {"error": "..."}
      - {"ok": False, "error": "..."}
      - {"result": {"error": "..."}}
      - {"result": {"ok": False, "error": "..."}}
    """
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
# Agent
# =============================================================================

class LightningAgent:
    """
    Persistent controller:
    - consumes runtime/agent/inbox.jsonl
    - writes runtime/agent/outbox.jsonl
    - all execution via MCP tools ONLY
    - typed commands are NO-LLM (cheap)
    - LLM is opt-in per request AND gated by ALLOW_LLM=1
    """

    def __init__(self) -> None:
        # Acquire single-instance lock immediately
        repo_root = Path(__file__).resolve().parents[1]
        lock_path = repo_root / "runtime" / "agent" / "agent.lock"
        self._lock = StartupLock(lock_path)
        self._lock.acquire_or_exit()

        self.mcp = FastMCPClient()
        self.backend = create_backend()

        self.tick_s = 0.5

        # LLM gating (deliberate spend)
        self.allow_llm = _env_bool("ALLOW_LLM", default=False)
        self.min_llm_interval_s = 1.0
        self._next_llm_time = _now_monotonic()
        self.max_steps_per_command = 12

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

    # -------------------------------------------------------------------------
    # CHEAP / TYPED COMMANDS (NO LLM)
    # -------------------------------------------------------------------------

    def _format_health(self, raw: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        inner = raw.get("result") if isinstance(raw, dict) and "result" in raw else raw
        if not isinstance(inner, dict):
            return ("Health: invalid response", {"status": "down"})

        status = str(inner.get("status", "unknown")).upper()
        network = inner.get("network", "regtest")

        btc_ok = bool(_safe_get(inner, "bitcoin", "ok", default=False))
        btc_payload = _safe_get(inner, "bitcoin", "payload", default={})
        blocks = _safe_get(btc_payload, "blocks", default="?")
        headers = _safe_get(btc_payload, "headers", default="?")
        ibd = _safe_get(btc_payload, "initialblockdownload", default="?")

        warnings = inner.get("warnings", [])
        if not isinstance(warnings, list):
            warnings = []

        nodes = inner.get("nodes", [])
        if not isinstance(nodes, list):
            nodes = []

        rows: List[List[str]] = []
        total_peers = 0
        total_active = 0
        total_utxos = 0
        total_fch = 0

        for n in nodes:
            if not isinstance(n, dict):
                continue

            name = str(n.get("name", "node-?"))
            gi_ok = bool(_safe_get(n, "getinfo", "ok", default=False))
            gi = _safe_get(n, "getinfo", "payload", default={}) if gi_ok else {}

            alias = str(gi.get("alias", "UNKNOWN")) if isinstance(gi, dict) else "UNKNOWN"
            nid = _short_id(str(gi.get("id", ""))) if isinstance(gi, dict) else "unknown"
            height = str(gi.get("blockheight", "?")) if isinstance(gi, dict) else "?"
            peers = str(gi.get("num_peers", "?")) if isinstance(gi, dict) else "?"
            active = str(gi.get("num_active_channels", "?")) if isinstance(gi, dict) else "?"
            host, port = _extract_binding_host_port(gi) if isinstance(gi, dict) else ("?", 0)

            funds_ok = bool(_safe_get(n, "funds", "ok", default=False))
            funds = _safe_get(n, "funds", "payload", default={}) if funds_ok else {}
            utxos = len(funds.get("outputs", [])) if isinstance(funds, dict) else 0
            fch = len(funds.get("channels", [])) if isinstance(funds, dict) else 0

            try:
                total_peers += int(peers)
            except Exception:
                pass
            try:
                total_active += int(active)
            except Exception:
                pass
            total_utxos += utxos
            total_fch += fch

            warn_flag = "!" if (gi_ok and isinstance(gi, dict) and len(_collect_node_warnings(gi)) > 0) else ""

            rows.append([
                name,
                "OK" if gi_ok else "DOWN",
                alias[:16],
                nid,
                f"{host}:{port}",
                height,
                peers,
                active,
                str(utxos),
                str(fch),
                warn_flag,
            ])

        nodes_total = _safe_get(inner, "summary", "nodes_total", default=len(rows))
        nodes_ok = _safe_get(inner, "summary", "nodes_ok", default=len([r for r in rows if r[1] == "OK"]))

        out: List[str] = []
        out.append(f"STATUS: {status}   NET: {network}")
        out.append(f"BITCOIN: {'OK' if btc_ok else 'DOWN'}   blocks={blocks} headers={headers} IBD={ibd}")
        out.append(f"NODES: {nodes_ok}/{nodes_total} OK   peers={total_peers}   active_ch={total_active}   utxos={total_utxos}   funded_ch={total_fch}")

        if warnings:
            out.append("")
            out.append(f"WARNINGS ({len(warnings)}):")
            for w in warnings:
                out.append(f" - {w}")

        out.append("")
        out.append("NODES:")
        out.append(_fmt_table(rows, headers=["NODE", "ST", "ALIAS", "ID", "ADDR", "HGT", "PEERS", "CH", "UTXO", "FCH", "W"]))

        hints: List[str] = []
        if total_peers == 0 and rows:
            hints.append("Connect nodes (no peers).")
        if total_utxos == 0 and total_fch == 0 and rows:
            hints.append("Fund node wallets (no UTXOs/channels).")
        if total_utxos > 0 and total_fch == 0 and rows:
            hints.append("Open channels (funded wallets but no channels).")

        if hints:
            out.append("")
            out.append("NEXT ACTIONS:")
            for h in hints:
                out.append(f" - {h}")

        summary = {
            "status": status.lower(),
            "network": network,
            "bitcoin_ok": btc_ok,
            "blocks": blocks,
            "headers": headers,
            "nodes_total": nodes_total,
            "nodes_ok": nodes_ok,
            "warnings": len(warnings),
        }
        return "\n".join(out), summary

    def _handle_health(self, req_id: int, include_raw: bool) -> None:
        raw = self.mcp.call("network_health")
        pretty, summary = self._format_health(raw)
        extra: Dict[str, Any] = {"summary": summary}
        if include_raw:
            extra["raw"] = raw
        self._write_report(req_id, pretty, extra=extra)

    def _handle_btc_info(self, req_id: int) -> None:
        res = self.mcp.call("btc_getblockchaininfo")
        self._write_report(req_id, json.dumps(res, indent=2, ensure_ascii=False))

    def _handle_btc_send(self, req_id: int, address: str, amount_btc: str) -> None:
        res = self.mcp.call("btc_sendtoaddress", address=address, amount_btc=amount_btc)
        self._write_report(req_id, json.dumps(res, indent=2, ensure_ascii=False))

    def _handle_btc_mine(self, req_id: int, blocks: int, address: str) -> None:
        res = self.mcp.call("btc_generatetoaddress", blocks=int(blocks), address=address)
        self._write_report(req_id, json.dumps(res, indent=2, ensure_ascii=False))

    def _handle_ln_getinfo(self, req_id: int, node: int) -> None:
        res = self.mcp.call("ln_getinfo", node=node)
        self._write_report(req_id, json.dumps(res, indent=2, ensure_ascii=False))

    def _handle_ln_listpeers(self, req_id: int, node: int) -> None:
        res = self.mcp.call("ln_listpeers", node=node)
        self._write_report(req_id, json.dumps(res, indent=2, ensure_ascii=False))

    def _handle_ln_listfunds(self, req_id: int, node: int) -> None:
        res = self.mcp.call("ln_listfunds", node=node)
        self._write_report(req_id, json.dumps(res, indent=2, ensure_ascii=False))

    def _handle_ln_listchannels(self, req_id: int, node: int) -> None:
        res = self.mcp.call("ln_listchannels", node=node)
        self._write_report(req_id, json.dumps(res, indent=2, ensure_ascii=False))

    def _handle_ln_newaddr(self, req_id: int, node: int) -> None:
        res = self.mcp.call("ln_newaddr", node=node)
        self._write_report(req_id, json.dumps(res, indent=2, ensure_ascii=False))

    def _handle_ln_connect(self, req_id: int, from_node: int, to_node: int) -> None:
        to_info = self.mcp.call("ln_getinfo", node=to_node)
        payload = _safe_get(to_info, "result", "payload", default=None) or _safe_get(to_info, "payload", default={})
        if not isinstance(payload, dict):
            self._write_report(req_id, f"ERROR: could not resolve node-{to_node} info")
            return

        peer_id = str(payload.get("id", ""))
        host, port = _extract_binding_host_port(payload)
        res = self.mcp.call("ln_connect", from_node=from_node, peer_id=peer_id, host=host, port=port)
        self._write_report(req_id, json.dumps(res, indent=2, ensure_ascii=False))

    def _handle_ln_openchannel(self, req_id: int, from_node: int, to_node: int, amount_sat: int) -> None:
        to_info = self.mcp.call("ln_getinfo", node=to_node)
        payload = _safe_get(to_info, "result", "payload", default=None) or _safe_get(to_info, "payload", default={})
        if not isinstance(payload, dict):
            self._write_report(req_id, f"ERROR: could not resolve node-{to_node} info")
            return

        peer_id = str(payload.get("id", ""))
        host, port = _extract_binding_host_port(payload)

        connect_res = self.mcp.call("ln_connect", from_node=from_node, peer_id=peer_id, host=host, port=port)
        open_res = self.mcp.call("ln_openchannel", from_node=from_node, peer_id=peer_id, amount_sat=int(amount_sat))

        self._write_report(req_id, json.dumps({"connect": connect_res, "openchannel": open_res}, indent=2, ensure_ascii=False))

    def _handle_ln_invoice(self, req_id: int, node: int, amount_msat: Optional[int], label: Optional[str], description: str) -> None:
        if not label:
            label = f"inv-{req_id}-{node}"
        res = self.mcp.call("ln_invoice", node=node, amount_msat=amount_msat, label=label, description=description)
        self._write_report(req_id, json.dumps(res, indent=2, ensure_ascii=False))

    def _handle_ln_pay(self, req_id: int, from_node: int, bolt11: str) -> None:
        res = self.mcp.call("ln_pay", from_node=from_node, bolt11=bolt11)
        self._write_report(req_id, json.dumps(res, indent=2, ensure_ascii=False))

    # -------------------------------------------------------------------------
    # LLM orchestration (deliberate)
    # -------------------------------------------------------------------------

    def _llm_allowed(self) -> bool:
        return _now_monotonic() >= self._next_llm_time

    def _reserve_llm(self) -> None:
        self._next_llm_time = max(self._next_llm_time, _now_monotonic() + self.min_llm_interval_s)

    def _llm_tools_full(self) -> List[Dict[str, Any]]:
        return [
            # Health
            {"type": "function", "function": {"name": "network_health", "description": "Check regtest Bitcoin+Lightning health.", "parameters": {"type": "object", "properties": {}}}},

            # Bitcoin
            {"type": "function", "function": {"name": "btc_getblockchaininfo", "description": "Get regtest blockchain status.", "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {"name": "btc_sendtoaddress", "description": "Send BTC (regtest).", "parameters": {"type": "object", "properties": {"address": {"type": "string"}, "amount_btc": {"type": "string"}}, "required": ["address", "amount_btc"]}}},
            {"type": "function", "function": {"name": "btc_generatetoaddress", "description": "Mine blocks (regtest).", "parameters": {"type": "object", "properties": {"blocks": {"type": "integer"}, "address": {"type": "string"}}, "required": ["blocks", "address"]}}},

            # Lightning read
            {"type": "function", "function": {"name": "ln_getinfo", "description": "Get CLN node info.", "parameters": {"type": "object", "properties": {"node": {"type": "integer"}}, "required": ["node"]}}},
            {"type": "function", "function": {"name": "ln_listpeers", "description": "List peers.", "parameters": {"type": "object", "properties": {"node": {"type": "integer"}}, "required": ["node"]}}},
            {"type": "function", "function": {"name": "ln_listfunds", "description": "List funds/channels.", "parameters": {"type": "object", "properties": {"node": {"type": "integer"}}, "required": ["node"]}}},
            {"type": "function", "function": {"name": "ln_listchannels", "description": "List peer channels (listpeerchannels).", "parameters": {"type": "object", "properties": {"node": {"type": "integer"}}, "required": ["node"]}}},
            {"type": "function", "function": {"name": "ln_newaddr", "description": "Get new on-chain address.", "parameters": {"type": "object", "properties": {"node": {"type": "integer"}}, "required": ["node"]}}},

            # Lightning actions
            {"type": "function", "function": {"name": "ln_connect", "description": "Connect from_node to peer_id@host:port.", "parameters": {"type": "object", "properties": {"from_node": {"type": "integer"}, "peer_id": {"type": "string"}, "host": {"type": "string"}, "port": {"type": "integer"}}, "required": ["from_node", "peer_id", "host", "port"]}}},
            {"type": "function", "function": {"name": "ln_openchannel", "description": "Open channel (fundchannel).", "parameters": {"type": "object", "properties": {"from_node": {"type": "integer"}, "peer_id": {"type": "string"}, "amount_sat": {"type": "integer"}}, "required": ["from_node", "peer_id", "amount_sat"]}}},
            {"type": "function", "function": {"name": "ln_invoice", "description": "Create invoice.", "parameters": {"type": "object", "properties": {"node": {"type": "integer"}, "amount_msat": {"type": "integer"}, "label": {"type": "string"}, "description": {"type": "string"}}, "required": ["node", "amount_msat", "label", "description"]}}},
            {"type": "function", "function": {"name": "ln_pay", "description": "Pay invoice.", "parameters": {"type": "object", "properties": {"from_node": {"type": "integer"}, "bolt11": {"type": "string"}}, "required": ["from_node", "bolt11"]}}},
        ]

    def _handle_freeform_llm(self, req_id: int, user_text: str) -> None:
        if not self.allow_llm:
            self._write_report(req_id, "LLM is disabled (ALLOW_LLM!=1). Enable it locally to run --llm requests.")
            return

        tools = self._llm_tools_full()
        tool_calls_made: List[str] = []

        system_prompt = (
            "You are a regtest Lightning controller.\n"
            "Hard rules:\n"
            "- You MUST only act via the provided MCP tools.\n"
            "- You MUST stop on any tool error.\n"
            "- Be concise and step-by-step.\n"
            "- If you need peer_id/host/port, call ln_getinfo(node) and read id + binding.\n"
        )
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ]

        steps = 0
        while steps < self.max_steps_per_command:
            steps += 1
            if not self._llm_allowed():
                time.sleep(0.1)
                continue
            self._reserve_llm()

            resp = self.backend.step(messages, tools)

            if resp.get("type") == "tool_call":
                tool_name = resp.get("tool_name")
                tool_args = resp.get("tool_args") or {}
                tool_calls_made.append(str(tool_name))

                result = self.mcp.call(str(tool_name), **tool_args)

                err = _is_tool_error(result)
                messages.append({"role": "assistant", "content": resp.get("reasoning") or ""})
                messages.append({"role": "tool", "name": str(tool_name), "content": json.dumps(result, ensure_ascii=False)})

                if err:
                    self._write_report(
                        req_id,
                        f"STOPPED: tool error in {tool_name}\nError: {err}\n\nRaw tool result:\n{json.dumps(result, indent=2, ensure_ascii=False)}",
                        extra={"used_llm": True, "steps": steps, "tool_calls": tool_calls_made},
                    )
                    return

                continue

            # final
            content = resp.get("content") or json.dumps(resp, ensure_ascii=False)
            self._write_report(req_id, content, extra={"used_llm": True, "steps": steps, "tool_calls": tool_calls_made})
            return

        self._write_report(req_id, "ERROR: exceeded max steps.", extra={"used_llm": True, "steps": steps, "tool_calls": tool_calls_made})

    # -------------------------------------------------------------------------
    # Main loop
    # -------------------------------------------------------------------------

    def run(self) -> None:
        self._log("agent_start", {"msg": "Agent online (single instance). Waiting for inbox commands."})

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

                    # Cheap / typed
                    if kind == "health_check":
                        self._handle_health(req_id, include_raw=bool(meta.get("include_raw", False)))

                    elif kind == "btc_info":
                        self._handle_btc_info(req_id)
                    elif kind == "btc_send":
                        self._handle_btc_send(req_id, address=str(meta["address"]), amount_btc=str(meta["amount_btc"]))
                    elif kind == "btc_mine":
                        self._handle_btc_mine(req_id, blocks=int(meta["blocks"]), address=str(meta["address"]))

                    elif kind == "ln_getinfo":
                        self._handle_ln_getinfo(req_id, node=int(meta["node"]))
                    elif kind == "ln_listpeers":
                        self._handle_ln_listpeers(req_id, node=int(meta["node"]))
                    elif kind == "ln_listfunds":
                        self._handle_ln_listfunds(req_id, node=int(meta["node"]))
                    elif kind == "ln_listchannels":
                        self._handle_ln_listchannels(req_id, node=int(meta["node"]))
                    elif kind == "ln_newaddr":
                        self._handle_ln_newaddr(req_id, node=int(meta["node"]))
                    elif kind == "ln_connect":
                        self._handle_ln_connect(req_id, from_node=int(meta["from_node"]), to_node=int(meta["to_node"]))
                    elif kind == "ln_openchannel":
                        self._handle_ln_openchannel(req_id, from_node=int(meta["from_node"]), to_node=int(meta["to_node"]), amount_sat=int(meta["amount_sat"]))
                    elif kind == "ln_invoice":
                        self._handle_ln_invoice(
                            req_id,
                            node=int(meta["node"]),
                            amount_msat=(None if meta.get("amount_msat") is None else int(meta["amount_msat"])),
                            label=(None if meta.get("label") in (None, "", "null") else str(meta["label"])),
                            description=str(meta.get("description", "invoice")),
                        )
                    elif kind == "ln_pay":
                        self._handle_ln_pay(req_id, from_node=int(meta["from_node"]), bolt11=str(meta["bolt11"]))

                    # Deliberate LLM orchestration
                    elif kind == "freeform" and bool(meta.get("use_llm", False)):
                        self._handle_freeform_llm(req_id, user_text=str(msg.get("content", "")))

                    else:
                        self._write_report(req_id, f"Unknown command kind: {kind}")

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
