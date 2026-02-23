#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# =============================================================================
# Deterministic path/env helpers
# =============================================================================

def _repo_root() -> Path:
    # mcp/ln_mcp_server.py -> mcp -> repo root
    return Path(__file__).resolve().parents[1]


def _env(name: str, default: str) -> str:
    v = os.getenv(name)
    return default if v is None or v.strip() == "" else v.strip()


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    return int(v)


@dataclass(frozen=True)
class RuntimeConfig:
    repo_root: Path
    runtime_dir: Path
    bitcoin_dir: Path
    lightning_base: Path
    bitcoin_rpc_port: int
    bitcoin_rpc_user: str
    bitcoin_rpc_password: str
    network: str  # regtest only
    cmd_timeout_s: int


def load_config() -> RuntimeConfig:
    root = _repo_root()
    runtime_dir = Path(_env("RUNTIME_DIR", str(root / "runtime")))
    bitcoin_dir = Path(_env("BITCOIN_DIR", str(runtime_dir / "bitcoin" / "shared")))
    lightning_base = Path(_env("LIGHTNING_BASE", str(runtime_dir / "lightning")))

    # Defaults match what your bitcoind/lightningd processes are currently using
    return RuntimeConfig(
        repo_root=root,
        runtime_dir=runtime_dir,
        bitcoin_dir=bitcoin_dir,
        lightning_base=lightning_base,
        bitcoin_rpc_port=_env_int("BITCOIN_RPC_PORT", 18443),
        bitcoin_rpc_user=_env("BITCOIN_RPC_USER", "lnrpc"),
        bitcoin_rpc_password=_env("BITCOIN_RPC_PASSWORD", "lnrpcpass"),
        network=_env("NETWORK", "regtest"),
        cmd_timeout_s=_env_int("MCP_CMD_TIMEOUT_S", 8),
    )


# =============================================================================
# Safe subprocess helpers (no shell, deterministic parsing)
# =============================================================================

def _run_json_cmd(argv: List[str], timeout_s: int) -> Tuple[bool, Dict[str, Any]]:
    """
    Runs a command expected to output JSON to stdout.
    Returns (ok, payload). On failure payload includes an 'error' string.
    """
    try:
        cp = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
            check=False,
            text=True,
        )
    except FileNotFoundError:
        return False, {"error": f"Command not found: {argv[0]}"}
    except subprocess.TimeoutExpired:
        return False, {"error": f"Timeout after {timeout_s}s: {' '.join(argv)}"}
    except Exception as e:
        return False, {"error": f"Exec error: {e.__class__.__name__}: {e}"}

    if cp.returncode != 0:
        err = (cp.stderr or cp.stdout or "").strip()
        return False, {"error": f"Non-zero exit ({cp.returncode}): {err}"}

    out = (cp.stdout or "").strip()
    if not out:
        return False, {"error": "Empty stdout (expected JSON)"}

    try:
        obj = json.loads(out)
        if isinstance(obj, dict):
            return True, obj
        return True, {"_raw": obj}
    except Exception:
        # If JSON parse fails, return raw output for debugging
        return False, {"error": "Invalid JSON output", "stdout": out[:2000]}


def _list_node_dirs(lightning_base: Path) -> List[Path]:
    if not lightning_base.exists():
        return []
    nodes = [p for p in lightning_base.iterdir() if p.is_dir() and p.name.startswith("node-")]
    # Deterministic ordering by numeric suffix if possible
    def key(p: Path) -> Tuple[int, str]:
        try:
            return (int(p.name.split("-", 1)[1]), p.name)
        except Exception:
            return (10**9, p.name)
    return sorted(nodes, key=key)


# =============================================================================
# Tool handlers
# =============================================================================

def _handle_bitcoin_health(cfg: RuntimeConfig) -> Dict[str, Any]:
    argv = [
        "bitcoin-cli",
        f"-{cfg.network}",
        f"-datadir={str(cfg.bitcoin_dir)}",
        f"-rpcport={cfg.bitcoin_rpc_port}",
        f"-rpcuser={cfg.bitcoin_rpc_user}",
        f"-rpcpassword={cfg.bitcoin_rpc_password}",
        "getblockchaininfo",
    ]
    ok, payload = _run_json_cmd(argv, cfg.cmd_timeout_s)
    return {"ok": ok, "payload": payload}


def _handle_cln_getinfo(cfg: RuntimeConfig, node_dir: Path) -> Dict[str, Any]:
    # IMPORTANT: must pass --network=regtest or lightning-cli will default to 'bitcoin'
    argv = [
        "lightning-cli",
        f"--network={cfg.network}",
        f"--lightning-dir={str(node_dir)}",
        "getinfo",
    ]
    ok, payload = _run_json_cmd(argv, cfg.cmd_timeout_s)
    return {"ok": ok, "payload": payload}


def _handle_cln_listpeers(cfg: RuntimeConfig, node_dir: Path) -> Dict[str, Any]:
    argv = [
        "lightning-cli",
        f"--network={cfg.network}",
        f"--lightning-dir={str(node_dir)}",
        "listpeers",
    ]
    ok, payload = _run_json_cmd(argv, cfg.cmd_timeout_s)
    return {"ok": ok, "payload": payload}


def _handle_cln_listfunds(cfg: RuntimeConfig, node_dir: Path) -> Dict[str, Any]:
    argv = [
        "lightning-cli",
        f"--network={cfg.network}",
        f"--lightning-dir={str(node_dir)}",
        "listfunds",
    ]
    ok, payload = _run_json_cmd(argv, cfg.cmd_timeout_s)
    return {"ok": ok, "payload": payload}


def _handle_network_health() -> Dict[str, Any]:
    """
    MCP tool: network_health
    Returns a deterministic structured health object.
    """
    cfg = load_config()

    result: Dict[str, Any] = {
        "status": "down",
        "network": cfg.network,
        "runtime_dir": str(cfg.runtime_dir),
        "bitcoin_dir": str(cfg.bitcoin_dir),
        "lightning_base": str(cfg.lightning_base),
        "bitcoin": {},
        "nodes": [],
        "summary": {},
        "warnings": [],
    }

    # --- Bitcoin health ---
    btc = _handle_bitcoin_health(cfg)
    result["bitcoin"] = btc
    bitcoin_ok = bool(btc.get("ok"))

    # --- CLN nodes ---
    node_dirs = _list_node_dirs(cfg.lightning_base)
    nodes_out: List[Dict[str, Any]] = []
    ok_nodes = 0

    for nd in node_dirs:
        node_entry: Dict[str, Any] = {
            "name": nd.name,
            "lightning_dir": str(nd),
            "getinfo": {},
            "peers": {},
            "funds": {},
        }

        gi = _handle_cln_getinfo(cfg, nd)
        node_entry["getinfo"] = gi

        if gi.get("ok"):
            ok_nodes += 1

            # Optional: these can be slower; still deterministic
            node_entry["peers"] = _handle_cln_listpeers(cfg, nd)
            node_entry["funds"] = _handle_cln_listfunds(cfg, nd)

        nodes_out.append(node_entry)

    result["nodes"] = nodes_out

    # --- Overall status determination (deterministic) ---
    total_nodes = len(node_dirs)
    if bitcoin_ok and total_nodes > 0 and ok_nodes == total_nodes:
        result["status"] = "ok"
    elif bitcoin_ok and ok_nodes > 0:
        result["status"] = "degraded"
        result["warnings"].append("Some nodes are not responding to lightning-cli getinfo")
    elif bitcoin_ok and total_nodes == 0:
        result["status"] = "degraded"
        result["warnings"].append("No node-* directories found under lightning_base")
    else:
        result["status"] = "down"
        if not bitcoin_ok:
            result["warnings"].append("bitcoind not responding to bitcoin-cli getblockchaininfo")

    result["summary"] = {
        "bitcoin_ok": bitcoin_ok,
        "nodes_total": total_nodes,
        "nodes_ok": ok_nodes,
    }

    return result


# =============================================================================
# Minimal JSON-RPC style dispatcher (compatible with your existing calls)
# =============================================================================

def _error(msg: str) -> Dict[str, Any]:
    return {"error": msg}


def handle(method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    In-process handler. Params are currently unused for network_health.
    """
    _ = params or {}
    if method == "network_health":
        return _handle_network_health()
    return _error(f"Unknown method '{method}'")


def handle_request(req: Dict[str, Any]) -> Dict[str, Any]:
    """
    Accepts requests like:
      {"id": 1, "method": "network_health", "params": {...}}
    Returns:
      {"id": 1, "result": {...}} OR {"id": 1, "error": "..."}
    """
    rid = req.get("id", 0)
    method = req.get("method")
    params = req.get("params") or {}

    if not isinstance(method, str):
        return {"id": rid, "error": "Invalid request: missing 'method' string"}

    result = handle(method, params)
    # Match your observed client behavior: include id + result
    if "error" in result and len(result.keys()) == 1:
        return {"id": rid, "error": result["error"]}
    return {"id": rid, "result": result}


def main() -> None:
    """
    STDIN/STDOUT JSON lines server.
    Reads one JSON request per line, writes one JSON response per line.
    """
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                resp = {"id": 0, "error": "Request must be a JSON object"}
            else:
                resp = handle_request(req)
        except Exception as e:
            resp = {"id": 0, "error": f"Parse/handle error: {e.__class__.__name__}: {e}"}

        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()