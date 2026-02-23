from __future__ import annotations

import json
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

from ai.command_queue import read_new, write_outbox
from ai.llm.factory import create_backend
from mcp.client.fastmcp import FastMCPClient


def _now_monotonic() -> float:
    import time as _t
    return _t.monotonic()


def _short_id(node_id: str, n: int = 12) -> str:
    if not isinstance(node_id, str) or not node_id:
        return "unknown"
    return node_id[:n]


def _trunc(s: Any, width: int) -> str:
    txt = "" if s is None else str(s)
    if width <= 0:
        return ""
    if len(txt) <= width:
        return txt.ljust(width)
    # Deterministic truncation
    if width <= 1:
        return txt[:width]
    return (txt[: width - 1] + "…")


def _extract_port(binding: Any) -> Optional[int]:
    # binding is typically a list of {"type","address","port"}
    if isinstance(binding, list):
        for b in binding:
            if isinstance(b, dict) and "port" in b:
                try:
                    return int(b["port"])
                except Exception:
                    continue
    return None


def _collect_node_warnings(getinfo_payload: Dict[str, Any]) -> List[str]:
    warns: List[str] = []
    for k, v in getinfo_payload.items():
        if isinstance(k, str) and k.startswith("warning_"):
            warns.append(f"{k}: {v}")
    return warns


def _safe_get(d: Any, *keys: str, default: Any = None) -> Any:
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


class LightningAgent:
    """
    Persistent AI controller:
    - waits for user commands (runtime/agent/inbox.jsonl)
    - HEALTH CHECK: MCP-only + deterministic formatter (NO LLM = cheap)
    - FREEFORM: may use LLM + MCP tools (only when you explicitly ask)
    """

    def __init__(self) -> None:
        self.mcp = FastMCPClient()
        self.backend = create_backend()

        # Deterministic cadence
        self.tick_s = 0.5

        # Minimum spacing between LLM calls (only used for freeform)
        self.min_llm_interval_s = 1.0
        self._next_llm_time = _now_monotonic()

        # Safety cap for tool-call loops per request (freeform only)
        self.max_steps_per_command = 6

    def _log(self, kind: str, payload: Dict[str, Any]) -> None:
        out = {"ts": int(time.time()), "kind": kind, **payload}
        print(json.dumps(out, ensure_ascii=False), flush=True)

    def _sleep_to_next_tick(self, start_t: float) -> None:
        elapsed = _now_monotonic() - start_t
        remain = max(0.0, self.tick_s - elapsed)
        time.sleep(remain)

    def _llm_allowed(self) -> bool:
        return _now_monotonic() >= self._next_llm_time

    def _reserve_llm(self) -> None:
        self._next_llm_time = max(self._next_llm_time, _now_monotonic() + self.min_llm_interval_s)

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

    # ---------------------------
    # Health reporting (NO LLM)
    # ---------------------------

    def _format_health_report(self, raw: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        """
        raw may be:
          {"id": X, "result": {...}}  (your MCP server pattern)
        or already the inner result dict.
        Returns (pretty_text, compact_summary_dict).
        """
        inner = raw.get("result") if isinstance(raw, dict) and "result" in raw else raw
        if not isinstance(inner, dict):
            return ("Health check failed: invalid response shape", {"status": "down"})

        status = str(inner.get("status", "unknown")).lower()
        network = inner.get("network", "regtest")

        # Bitcoin
        btc_ok = bool(_safe_get(inner, "bitcoin", "ok", default=False))
        btc_payload = _safe_get(inner, "bitcoin", "payload", default={})
        blocks = _safe_get(btc_payload, "blocks", default=None)
        headers = _safe_get(btc_payload, "headers", default=None)
        ibd = _safe_get(btc_payload, "initialblockdownload", default=None)
        btc_warn = _safe_get(btc_payload, "warnings", default="")

        # Nodes
        nodes = inner.get("nodes", [])
        if not isinstance(nodes, list):
            nodes = []

        total_nodes = _safe_get(inner, "summary", "nodes_total", default=len(nodes))
        ok_nodes = _safe_get(inner, "summary", "nodes_ok", default=0)

        # Aggregates for quick glance
        total_peers = 0
        total_active_ch = 0
        total_utxos = 0
        total_funded_ch = 0

        warnings: List[str] = []

        # Build node table rows
        rows: List[Dict[str, Any]] = []

        for n in nodes:
            if not isinstance(n, dict):
                continue

            name = n.get("name", "node-?")
            gi_ok = bool(_safe_get(n, "getinfo", "ok", default=False))
            gi = _safe_get(n, "getinfo", "payload", default={}) if gi_ok else {}

            alias = gi.get("alias", "UNKNOWN") if isinstance(gi, dict) else "UNKNOWN"
            node_id = gi.get("id", "") if isinstance(gi, dict) else ""
            height = gi.get("blockheight", None) if isinstance(gi, dict) else None
            port = _extract_port(gi.get("binding")) if isinstance(gi, dict) else None

            peers_count = None
            if gi_ok and isinstance(gi, dict) and "num_peers" in gi:
                try:
                    peers_count = int(gi["num_peers"])
                except Exception:
                    peers_count = None

            active_ch = None
            if gi_ok and isinstance(gi, dict) and "num_active_channels" in gi:
                try:
                    active_ch = int(gi["num_active_channels"])
                except Exception:
                    active_ch = None

            # Funds summary
            funds_ok = bool(_safe_get(n, "funds", "ok", default=False))
            funds_payload = _safe_get(n, "funds", "payload", default={}) if funds_ok else {}
            outputs_n = len(funds_payload.get("outputs", [])) if isinstance(funds_payload, dict) else 0
            chans_n = len(funds_payload.get("channels", [])) if isinstance(funds_payload, dict) else 0

            # Aggregate totals
            total_peers += int(peers_count or 0)
            total_active_ch += int(active_ch or 0)
            total_utxos += int(outputs_n)
            total_funded_ch += int(chans_n)

            # Collect node warnings from getinfo payload
            node_warns: List[str] = []
            if gi_ok and isinstance(gi, dict):
                node_warns = _collect_node_warnings(gi)
                for w in node_warns:
                    warnings.append(f"{name} {w}")

            rows.append(
                {
                    "name": str(name),
                    "ok": gi_ok,
                    "alias": str(alias),
                    "id_short": _short_id(str(node_id)),
                    "port": port,
                    "height": height,
                    "peers": peers_count,
                    "active_ch": active_ch,
                    "utxos": outputs_n,
                    "funded_ch": chans_n,
                    "warn_count": len(node_warns),
                }
            )

        # Bitcoin warnings
        if isinstance(btc_warn, str) and btc_warn.strip():
            warnings.append(f"bitcoin warnings: {btc_warn.strip()}")

        # ---- Build at-a-glance report ----
        status_word = status.upper()

        # Big summary lines (glanceable)
        line1 = f"STATUS: {status_word}   NET: {network}"
        btc_state = "OK" if btc_ok else "DOWN"
        line2 = f"BITCOIN: {btc_state}   blocks={blocks} headers={headers} IBD={ibd}"
        line3 = f"NODES: {ok_nodes}/{total_nodes} OK   peers={total_peers}   active_ch={total_active_ch}   utxos={total_utxos}   funded_ch={total_funded_ch}"

        # Warnings at top (so you don't miss them)
        pretty: List[str] = []
        pretty.append(line1)
        pretty.append(line2)
        pretty.append(line3)

        if warnings:
            pretty.append("")
            pretty.append(f"WARNINGS ({len(warnings)}):")
            # keep deterministic order: already built in node iteration order
            for w in warnings:
                pretty.append(f" - {w}")

        # Node table
        pretty.append("")
        pretty.append("NODES:")
        header = (
            f"{_trunc('NODE', 8)} "
            f"{_trunc('STATUS', 6)} "
            f"{_trunc('ALIAS', 16)} "
            f"{_trunc('ID', 12)} "
            f"{_trunc('PORT', 5)} "
            f"{_trunc('HEIGHT', 6)} "
            f"{_trunc('PEERS', 5)} "
            f"{_trunc('CH', 3)} "
            f"{_trunc('UTXO', 4)} "
            f"{_trunc('FCH', 3)} "
            f"{_trunc('W', 1)}"
        )
        pretty.append(header)
        pretty.append("-" * len(header))

        if not rows:
            pretty.append("(no nodes detected)")
        else:
            for r in rows:
                st = "OK" if r["ok"] else "DOWN"
                port = "" if r["port"] is None else str(r["port"])
                height = "" if r["height"] is None else str(r["height"])
                peers = "" if r["peers"] is None else str(r["peers"])
                ch = "" if r["active_ch"] is None else str(r["active_ch"])
                utxo = str(r["utxos"])
                fch = str(r["funded_ch"])
                wflag = "!" if int(r["warn_count"]) > 0 else ""

                pretty.append(
                    f"{_trunc(r['name'], 8)} "
                    f"{_trunc(st, 6)} "
                    f"{_trunc(r['alias'], 16)} "
                    f"{_trunc(r['id_short'], 12)} "
                    f"{_trunc(port, 5)} "
                    f"{_trunc(height, 6)} "
                    f"{_trunc(peers, 5)} "
                    f"{_trunc(ch, 3)} "
                    f"{_trunc(utxo, 4)} "
                    f"{_trunc(fch, 3)} "
                    f"{_trunc(wflag, 1)}"
                )

        # Helpful (deterministic) “next actions” hints for common states
        next_actions: List[str] = []
        if total_nodes and total_peers == 0:
            next_actions.append("Connect nodes (no peers).")
        if total_nodes and total_utxos == 0 and total_funded_ch == 0:
            next_actions.append("Fund node wallets (no UTXOs/channels).")
        if total_nodes and total_funded_ch == 0 and total_utxos > 0:
            next_actions.append("Open channels (funded wallets but no channels).")

        if next_actions:
            pretty.append("")
            pretty.append("NEXT ACTIONS:")
            for a in next_actions:
                pretty.append(f" - {a}")

        summary = {
            "status": status,
            "network": network,
            "bitcoin_ok": btc_ok,
            "blocks": blocks,
            "headers": headers,
            "ibd": ibd,
            "nodes_total": total_nodes,
            "nodes_ok": ok_nodes,
            "peers_total": total_peers,
            "active_channels_total": total_active_ch,
            "utxos_total": total_utxos,
            "funded_channels_total": total_funded_ch,
            "warnings_count": len(warnings),
        }

        return ("\n".join(pretty), summary)

    def _handle_health_check(self, req: Dict[str, Any]) -> None:
        req_id = int(req.get("id", 0))
        meta = req.get("meta") or {}
        include_raw = bool(meta.get("include_raw", False))

        self._log("health_check_start", {"request_id": req_id})

        raw = self.mcp.call("network_health")
        pretty, summary = self._format_health_report(raw)

        extra: Dict[str, Any] = {"summary": summary}
        if include_raw:
            extra["raw"] = raw  # optional debug (still no LLM)

        self._write_report(req_id, pretty, extra=extra)
        self._log("health_check_done", {"request_id": req_id, **summary})

    # ---------------------------
    # Freeform (may use LLM)
    # ---------------------------

    def _handle_freeform(self, req: Dict[str, Any]) -> None:
        """
        Freeform path: may use LLM + MCP tools.
        Only runs when you explicitly enqueue via `ai.cli ask "..."`
        """
        req_id = int(req.get("id", 0))
        user_text = str(req.get("content", ""))

        self._log("freeform_start", {"request_id": req_id})

        messages: List[Dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "You are a Lightning Network (regtest) controller.\n"
                    "Rules:\n"
                    "- You MUST only act using MCP tools.\n"
                    "- You MUST NOT call Lightning RPC directly.\n"
                    "- You MUST NOT bypass MCP.\n"
                ),
            },
            {"role": "user", "content": user_text},
        ]

        tools = [
            {
                "type": "function",
                "function": {
                    "name": "network_health",
                    "description": "Check health of Bitcoin+Lightning regtest network.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

        steps = 0
        while steps < self.max_steps_per_command:
            steps += 1

            if not self._llm_allowed():
                time.sleep(0.1)
                continue

            self._reserve_llm()

            resp = self.backend.step(messages, tools)

            if resp["type"] == "tool_call":
                tool_name = resp["tool_name"]
                tool_args = resp["tool_args"] or {}

                result = self.mcp.call(tool_name, **tool_args)

                messages.append({"role": "assistant", "content": resp.get("reasoning") or ""})
                messages.append({"role": "tool", "name": tool_name, "content": json.dumps(result, ensure_ascii=False)})
                continue

            content = resp.get("content") or ""
            messages.append({"role": "assistant", "content": content})
            self._write_report(req_id, content, extra={"steps": steps})
            self._log("freeform_done", {"request_id": req_id, "steps": steps})
            return

        self._write_report(req_id, "ERROR: exceeded max steps for this request.", extra={"steps": steps})
        self._log("freeform_max_steps", {"request_id": req_id, "steps": steps})

    # ---------------------------
    # Main loop
    # ---------------------------

    def run(self) -> None:
        self._log("agent_start", {"msg": "Agent online. Waiting for runtime/agent/inbox.jsonl"})

        while True:
            tick_start = _now_monotonic()
            try:
                new_msgs = read_new()
                if not new_msgs:
                    self._sleep_to_next_tick(tick_start)
                    continue

                for msg in new_msgs:
                    meta = msg.get("meta") or {}
                    kind = meta.get("kind")

                    if kind == "health_check":
                        self._handle_health_check(msg)
                    else:
                        self._handle_freeform(msg)

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