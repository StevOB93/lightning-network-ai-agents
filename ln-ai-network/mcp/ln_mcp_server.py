#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union


# =============================================================================
# Deterministic config
# =============================================================================

def _repo_root() -> Path:
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
    network: str
    cmd_timeout_s: int


def load_config() -> RuntimeConfig:
    root = _repo_root()
    runtime_dir = Path(_env("RUNTIME_DIR", str(root / "runtime")))
    bitcoin_dir = Path(_env("BITCOIN_DIR", str(runtime_dir / "bitcoin" / "shared")))
    lightning_base = Path(_env("LIGHTNING_BASE", str(runtime_dir / "lightning")))

    return RuntimeConfig(
        repo_root=root,
        runtime_dir=runtime_dir,
        bitcoin_dir=bitcoin_dir,
        lightning_base=lightning_base,
        bitcoin_rpc_port=_env_int("BITCOIN_RPC_PORT", 18443),
        bitcoin_rpc_user=_env("BITCOIN_RPC_USER", "lnrpc"),
        bitcoin_rpc_password=_env("BITCOIN_RPC_PASSWORD", "lnrpcpass"),
        network=_env("NETWORK", "regtest"),
        cmd_timeout_s=_env_int("MCP_CMD_TIMEOUT_S", 10),
    )


# =============================================================================
# Subprocess helpers (no shell, deterministic)
# =============================================================================

def _run_cmd(argv: List[str], timeout_s: int) -> Tuple[int, str, str]:
    try:
        cp = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
            check=False,
            text=True,
        )
        return cp.returncode, (cp.stdout or "").strip(), (cp.stderr or "").strip()
    except FileNotFoundError:
        return 127, "", f"Command not found: {argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"Timeout after {timeout_s}s: {' '.join(argv)}"
    except Exception as e:
        return 125, "", f"Exec error: {e.__class__.__name__}: {e}"


def _run_json(argv: List[str], timeout_s: int) -> Dict[str, Any]:
    rc, out, err = _run_cmd(argv, timeout_s)
    if rc != 0:
        return {"ok": False, "error": f"Non-zero exit ({rc}): {err or out}", "argv": argv}
    if out == "":
        return {"ok": False, "error": "Empty stdout (expected JSON)", "argv": argv}
    try:
        obj = json.loads(out)
        return {"ok": True, "payload": obj}
    except Exception:
        return {"ok": False, "error": "Invalid JSON output", "stdout": out[:2000], "argv": argv}


def _run_text(argv: List[str], timeout_s: int) -> Dict[str, Any]:
    rc, out, err = _run_cmd(argv, timeout_s)
    if rc != 0:
        return {"ok": False, "error": f"Non-zero exit ({rc}): {err or out}", "argv": argv}
    return {"ok": True, "payload": out}


# =============================================================================
# CLN node directory resolution
# =============================================================================

def _list_node_dirs(lightning_base: Path) -> List[Path]:
    if not lightning_base.exists():
        return []

    nodes = [p for p in lightning_base.iterdir() if p.is_dir() and p.name.startswith("node-")]

    def key(p: Path) -> Tuple[int, str]:
        try:
            return (int(p.name.split("-", 1)[1]), p.name)
        except Exception:
            return (10**9, p.name)

    return sorted(nodes, key=key)


def _node_dir(cfg: RuntimeConfig, node: Union[int, str]) -> Path:
    # node can be 1 or "node-1"
    if isinstance(node, int):
        name = f"node-{node}"
    else:
        node_s = str(node)
        name = node_s if node_s.startswith("node-") else f"node-{node_s}"
    return cfg.lightning_base / name


def _require_node_dir(cfg: RuntimeConfig, node: Union[int, str]) -> Path:
    nd = _node_dir(cfg, node)
    if not nd.exists():
        raise ValueError(f"Node dir does not exist: {nd}")
    return nd


# =============================================================================
# Bitcoin helpers
# =============================================================================

def _btc_base(cfg: RuntimeConfig) -> List[str]:
    return [
        "bitcoin-cli",
        f"-{cfg.network}",
        f"-datadir={str(cfg.bitcoin_dir)}",
        f"-rpcport={cfg.bitcoin_rpc_port}",
        f"-rpcuser={cfg.bitcoin_rpc_user}",
        f"-rpcpassword={cfg.bitcoin_rpc_password}",
    ]


def btc_getblockchaininfo() -> Dict[str, Any]:
    cfg = load_config()
    return _run_json(_btc_base(cfg) + ["getblockchaininfo"], cfg.cmd_timeout_s)


def btc_sendtoaddress(address: str, amount_btc: str) -> Dict[str, Any]:
    cfg = load_config()
    # sendtoaddress returns a txid (text), not JSON
    return _run_text(_btc_base(cfg) + ["sendtoaddress", address, str(amount_btc)], cfg.cmd_timeout_s)


def btc_generatetoaddress(blocks: int, address: str) -> Dict[str, Any]:
    cfg = load_config()
    # generatetoaddress returns JSON array of block hashes
    return _run_json(_btc_base(cfg) + ["generatetoaddress", str(int(blocks)), address], cfg.cmd_timeout_s)


# =============================================================================
# Lightning helpers
# =============================================================================

def _ln_base(cfg: RuntimeConfig, node_dir: Path) -> List[str]:
    # IMPORTANT: must pass --network=regtest or lightning-cli defaults to "bitcoin"
    return [
        "lightning-cli",
        f"--network={cfg.network}",
        f"--lightning-dir={str(node_dir)}",
    ]


def ln_getinfo(node: Union[int, str]) -> Dict[str, Any]:
    cfg = load_config()
    nd = _require_node_dir(cfg, node)
    return _run_json(_ln_base(cfg, nd) + ["getinfo"], cfg.cmd_timeout_s)


def ln_listpeers(node: Union[int, str]) -> Dict[str, Any]:
    cfg = load_config()
    nd = _require_node_dir(cfg, node)
    return _run_json(_ln_base(cfg, nd) + ["listpeers"], cfg.cmd_timeout_s)


def ln_listfunds(node: Union[int, str]) -> Dict[str, Any]:
    cfg = load_config()
    nd = _require_node_dir(cfg, node)
    return _run_json(_ln_base(cfg, nd) + ["listfunds"], cfg.cmd_timeout_s)


def ln_listchannels(node: Union[int, str]) -> Dict[str, Any]:
    """
    In CLN, 'listchannels' is global gossip.
    For local channel state, listpeerchannels is usually what you want.
    We'll map ln_listchannels -> listpeerchannels with no args (lists all peer channels).
    """
    cfg = load_config()
    nd = _require_node_dir(cfg, node)
    return _run_json(_ln_base(cfg, nd) + ["listpeerchannels"], cfg.cmd_timeout_s)


def ln_newaddr(node: Union[int, str]) -> Dict[str, Any]:
    cfg = load_config()
    nd = _require_node_dir(cfg, node)
    return _run_json(_ln_base(cfg, nd) + ["newaddr"], cfg.cmd_timeout_s)


def ln_connect(from_node: Union[int, str], peer_id: str, host: str, port: int) -> Dict[str, Any]:
    cfg = load_config()
    nd = _require_node_dir(cfg, from_node)
    target = f"{peer_id}@{host}:{int(port)}"
    return _run_json(_ln_base(cfg, nd) + ["connect", target], cfg.cmd_timeout_s)


def ln_openchannel(from_node: Union[int, str], peer_id: str, amount_sat: int) -> Dict[str, Any]:
    """
    Maps to CLN 'fundchannel <id> <amount_sat>'.
    """
    cfg = load_config()
    nd = _require_node_dir(cfg, from_node)
    return _run_json(_ln_base(cfg, nd) + ["fundchannel", peer_id, str(int(amount_sat))], cfg.cmd_timeout_s)


def ln_invoice(node: Union[int, str], amount_msat: Optional[int], label: str, description: str) -> Dict[str, Any]:
    """
    CLN: invoice <msatoshi|any> <label> <description>
    """
    cfg = load_config()
    nd = _require_node_dir(cfg, node)
    amt = "any" if amount_msat is None else str(int(amount_msat))
    return _run_json(_ln_base(cfg, nd) + ["invoice", amt, label, description], cfg.cmd_timeout_s)


def ln_pay(from_node: Union[int, str], bolt11: str) -> Dict[str, Any]:
    cfg = load_config()
    nd = _require_node_dir(cfg, from_node)
    return _run_json(_ln_base(cfg, nd) + ["pay", bolt11], cfg.cmd_timeout_s)


# =============================================================================
# network_health (informative, deterministic)
# =============================================================================

def network_health() -> Dict[str, Any]:
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

    btc = btc_getblockchaininfo()
    result["bitcoin"] = btc
    bitcoin_ok = bool(btc.get("ok"))

    node_dirs = _list_node_dirs(cfg.lightning_base)
    ok_nodes = 0
    nodes_out: List[Dict[str, Any]] = []
    warnings: List[str] = []

    for nd in node_dirs:
        name = nd.name
        gi = ln_getinfo(name)
        peers = {"ok": False, "error": "skipped"}
        funds = {"ok": False, "error": "skipped"}
        ch = {"ok": False, "error": "skipped"}

        if gi.get("ok"):
            ok_nodes += 1
            peers = ln_listpeers(name)
            funds = ln_listfunds(name)
            ch = ln_listchannels(name)

            payload = gi.get("payload", {})
            if isinstance(payload, dict):
                for k, v in payload.items():
                    if isinstance(k, str) and k.startswith("warning_"):
                        warnings.append(f"{name} {k}: {v}")

        nodes_out.append(
            {
                "name": name,
                "lightning_dir": str(nd),
                "getinfo": gi,
                "peers": peers,
                "funds": funds,
                "channels": ch,
            }
        )

    result["nodes"] = nodes_out
    total_nodes = len(node_dirs)

    # Overall status logic
    if bitcoin_ok and total_nodes > 0 and ok_nodes == total_nodes:
        result["status"] = "ok"
    elif bitcoin_ok and ok_nodes > 0:
        result["status"] = "degraded"
        warnings.append("Some nodes are not responding to getinfo")
    elif bitcoin_ok and total_nodes == 0:
        result["status"] = "degraded"
        warnings.append("No node-* dirs found under lightning_base")
    else:
        result["status"] = "down"
        if not bitcoin_ok:
            warnings.append("bitcoind not responding")

    result["warnings"] = warnings
    result["summary"] = {
        "bitcoin_ok": bitcoin_ok,
        "nodes_total": total_nodes,
        "nodes_ok": ok_nodes,
        "warnings_count": len(warnings),
    }

    return result


# =============================================================================
# Dispatcher (method -> handler)
# =============================================================================

def _error(msg: str) -> Dict[str, Any]:
    return {"error": msg}


def handle(method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    params = params or {}

    try:
        if method == "network_health":
            return network_health()

        # Bitcoin
        if method == "btc_getblockchaininfo":
            return btc_getblockchaininfo()
        if method == "btc_sendtoaddress":
            return btc_sendtoaddress(str(params["address"]), str(params["amount_btc"]))
        if method == "btc_generatetoaddress":
            return btc_generatetoaddress(int(params["blocks"]), str(params["address"]))

        # Lightning (read-only)
        if method == "ln_getinfo":
            return ln_getinfo(params["node"])
        if method == "ln_listpeers":
            return ln_listpeers(params["node"])
        if method == "ln_listfunds":
            return ln_listfunds(params["node"])
        if method == "ln_listchannels":
            return ln_listchannels(params["node"])
        if method == "ln_newaddr":
            return ln_newaddr(params["node"])

        # Lightning (actions)
        if method == "ln_connect":
            return ln_connect(params["from_node"], str(params["peer_id"]), str(params["host"]), int(params["port"]))
        if method == "ln_openchannel":
            return ln_openchannel(params["from_node"], str(params["peer_id"]), int(params["amount_sat"]))
        if method == "ln_invoice":
            amt = params.get("amount_msat", None)
            amt_i = None if amt is None else int(amt)
            return ln_invoice(params["node"], amt_i, str(params["label"]), str(params["description"]))
        if method == "ln_pay":
            return ln_pay(params["from_node"], str(params["bolt11"]))

        return _error(f"Unknown method '{method}'")

    except KeyError as e:
        return _error(f"Missing required param: {e}")
    except ValueError as e:
        return _error(str(e))
    except Exception as e:
        return _error(f"Unhandled error: {e.__class__.__name__}: {e}")


def handle_request(req: Dict[str, Any]) -> Dict[str, Any]:
    rid = req.get("id", 0)
    method = req.get("method")
    params = req.get("params") or {}

    if not isinstance(method, str):
        return {"id": rid, "error": "Invalid request: missing 'method' string"}

    res = handle(method, params)
    if "error" in res and len(res.keys()) == 1:
        return {"id": rid, "error": res["error"]}
    return {"id": rid, "result": res}


def main() -> None:
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