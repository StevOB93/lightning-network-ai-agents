#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union


# =============================================================================
# Config
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

    lightning_base_port: int
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
        lightning_base_port=_env_int("LIGHTNING_BASE_PORT", 9735),
        network=_env("NETWORK", "regtest"),
        cmd_timeout_s=_env_int("MCP_CMD_TIMEOUT_S", 10),
    )


# =============================================================================
# Utilities
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


def _looks_like_node_not_running(err: str) -> bool:
    e = (err or "").lower()
    needles = [
        "connection refused",
        "lightning-rpc",
        "cannot connect",
        "failed to connect",
        "rpc",
    ]
    return any(n in e for n in needles)


def _node_index(node: Union[int, str]) -> int:
    if isinstance(node, int):
        idx = node
    else:
        s = str(node)
        if s.startswith("node-"):
            s = s.split("-", 1)[1]
        idx = int(s)

    # IMPORTANT: 1-based node indexing (node-1, node-2, ...)
    if idx < 1:
        raise ValueError(f"Invalid node index {idx}. Nodes are 1-based (node-1, node-2, ...).")
    return idx


def _node_dir(cfg: RuntimeConfig, node: Union[int, str]) -> Path:
    idx = _node_index(node)
    return cfg.lightning_base / f"node-{idx}"


def _require_node_dir(cfg: RuntimeConfig, node: Union[int, str]) -> Path:
    nd = _node_dir(cfg, node)
    if not nd.exists():
        raise ValueError(f"Node dir does not exist: {nd}")
    return nd


def _node_port(cfg: RuntimeConfig, node: Union[int, str]) -> int:
    idx = _node_index(node)
    return int(cfg.lightning_base_port + idx - 1)


def _ln_base(cfg: RuntimeConfig, node_dir: Path) -> List[str]:
    return ["lightning-cli", f"--network={cfg.network}", f"--lightning-dir={str(node_dir)}"]


def _btc_base(cfg: RuntimeConfig, wallet: Optional[str] = None) -> List[str]:
    argv = [
        "bitcoin-cli",
        f"-{cfg.network}",
        f"-datadir={str(cfg.bitcoin_dir)}",
        f"-rpcport={cfg.bitcoin_rpc_port}",
        f"-rpcuser={cfg.bitcoin_rpc_user}",
        f"-rpcpassword={cfg.bitcoin_rpc_password}",
    ]
    # Backward compatible: only add -rpcwallet when provided (or defaulted).
    if wallet:
        argv.append(f"-rpcwallet={wallet}")
    return argv


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


# =============================================================================
# Tool listing
# =============================================================================

def list_tools() -> Dict[str, Any]:
    tools = [
        "list_tools",
        "network_health",
        "btc_getblockchaininfo",
        "btc_wallet_ensure",
        "btc_getnewaddress",
        "btc_sendtoaddress",
        "btc_generatetoaddress",
        "ln_listnodes",
        "ln_node_create",
        "ln_node_status",
        "ln_node_start",
        "ln_node_stop",
        "ln_node_delete",
        "ln_getinfo",
        "ln_listpeers",
        "ln_listfunds",
        "ln_listchannels",
        "ln_newaddr",
        "ln_connect",
        "ln_openchannel",
        "ln_invoice",
        "ln_pay",
    ]
    return {"ok": True, "payload": {"tools": tools, "count": len(tools)}}


# =============================================================================
# Bitcoin tools
# =============================================================================

def btc_getblockchaininfo() -> Dict[str, Any]:
    cfg = load_config()
    return _run_json(_btc_base(cfg) + ["getblockchaininfo"], cfg.cmd_timeout_s)


def btc_wallet_ensure(wallet_name: str) -> Dict[str, Any]:
    cfg = load_config()
    listdir = _run_json(_btc_base(cfg) + ["listwalletdir"], cfg.cmd_timeout_s)
    if not listdir.get("ok"):
        return listdir

    wallets = listdir.get("payload", {}).get("wallets", [])
    exists = any(isinstance(w, dict) and w.get("name") == wallet_name for w in wallets)
    if not exists:
        created = _run_json(_btc_base(cfg) + ["createwallet", wallet_name], cfg.cmd_timeout_s)
        if not created.get("ok"):
            return created

    _run_json(_btc_base(cfg) + ["loadwallet", wallet_name], cfg.cmd_timeout_s)
    return {"ok": True, "payload": {"wallet": wallet_name, "ensured": True}}


def btc_getnewaddress(wallet: Optional[str] = None) -> Dict[str, Any]:
    cfg = load_config()
    return _run_text(_btc_base(cfg, wallet=wallet) + ["getnewaddress"], cfg.cmd_timeout_s)


def btc_sendtoaddress(address: str, amount_btc: str, wallet: Optional[str] = "miner") -> Dict[str, Any]:
    """
    Backward compatible:
      - existing callers pass (address, amount_btc)
      - new callers may pass wallet; default is 'miner' to avoid Core wallet -19 error
    """
    cfg = load_config()
    w = wallet if wallet is not None else "miner"
    return _run_text(_btc_base(cfg, wallet=w) + ["sendtoaddress", address, str(amount_btc)], cfg.cmd_timeout_s)


def btc_generatetoaddress(blocks: int, address: str) -> Dict[str, Any]:
    cfg = load_config()
    return _run_json(_btc_base(cfg) + ["generatetoaddress", str(int(blocks)), address], cfg.cmd_timeout_s)


# =============================================================================
# Node lifecycle
# =============================================================================

def ln_listnodes() -> Dict[str, Any]:
    cfg = load_config()
    nodes = _list_node_dirs(cfg.lightning_base)
    return {"ok": True, "payload": {"lightning_base": str(cfg.lightning_base), "nodes": [p.name for p in nodes], "count": len(nodes)}}


def ln_node_create(node: Union[int, str]) -> Dict[str, Any]:
    cfg = load_config()
    nd = _node_dir(cfg, node)
    nd.mkdir(parents=True, exist_ok=True)
    return {"ok": True, "payload": {"node": nd.name, "lightning_dir": str(nd)}}


def ln_node_status(node: Union[int, str]) -> Dict[str, Any]:
    cfg = load_config()
    nd = _node_dir(cfg, node)
    if not nd.exists():
        return {"ok": False, "error": f"Node dir does not exist: {nd}"}

    gi = _run_json(_ln_base(cfg, nd) + ["getinfo"], cfg.cmd_timeout_s)
    if gi.get("ok"):
        return {"ok": True, "payload": {"node": nd.name, "running": True}}

    err = str(gi.get("error") or "")
    if _looks_like_node_not_running(err):
        return {"ok": True, "payload": {"node": nd.name, "running": False, "reason": err}}
    return {"ok": False, "error": err, "payload": {"node": nd.name}}


def ln_node_start(node: Union[int, str]) -> Dict[str, Any]:
    cfg = load_config()
    nd = _node_dir(cfg, node)
    nd.mkdir(parents=True, exist_ok=True)

    st = ln_node_status(node)
    if st.get("ok") and st.get("payload", {}).get("running") is True:
        return {"ok": True, "payload": {"node": nd.name, "started": False, "running": True}}

    port = _node_port(cfg, node)
    log_file = nd / "lightningd.log"

    argv = [
        "lightningd",
        f"--network={cfg.network}",
        f"--lightning-dir={str(nd)}",
        f"--addr=127.0.0.1:{port}",
        "--bitcoin-rpcconnect=127.0.0.1",
        f"--bitcoin-rpcport={cfg.bitcoin_rpc_port}",
        f"--bitcoin-rpcuser={cfg.bitcoin_rpc_user}",
        f"--bitcoin-rpcpassword={cfg.bitcoin_rpc_password}",
        f"--bitcoin-datadir={str(cfg.bitcoin_dir)}",
        f"--log-file={str(log_file)}",
    ]

    try:
        with log_file.open("a", encoding="utf-8") as lf:
            subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=lf,
                stderr=lf,
                text=True,
                start_new_session=True,
            )
    except FileNotFoundError:
        return {"ok": False, "error": "Command not found: lightningd", "argv": argv}
    except Exception as e:
        return {"ok": False, "error": f"Failed to start lightningd: {e.__class__.__name__}: {e}", "argv": argv}

    deadline = time.time() + 15.0
    last_reason = ""
    while time.time() < deadline:
        st2 = ln_node_status(node)
        if st2.get("ok") and st2.get("payload", {}).get("running") is True:
            return {"ok": True, "payload": {"node": nd.name, "started": True, "running": True, "port": port, "log_file": str(log_file)}}
        last_reason = str(st2.get("payload", {}).get("reason", "")) or str(st2.get("error", ""))
        time.sleep(0.5)

    return {"ok": False, "error": f"lightningd started but RPC not ready within 15s: {last_reason}", "payload": {"node": nd.name, "port": port, "log_file": str(log_file)}}


def ln_node_stop(node: Union[int, str]) -> Dict[str, Any]:
    cfg = load_config()
    nd = _node_dir(cfg, node)
    if not nd.exists():
        return {"ok": True, "payload": {"node": str(node), "stopped": False, "reason": "node dir missing"}}

    res = _run_json(_ln_base(cfg, nd) + ["stop"], cfg.cmd_timeout_s)

    deadline = time.time() + 15.0
    while time.time() < deadline:
        st = ln_node_status(node)
        if st.get("ok") and st.get("payload", {}).get("running") is False:
            return {"ok": True, "payload": {"node": nd.name, "stopped": True, "stop_result": res}}
        time.sleep(0.5)

    return {"ok": False, "error": "Node did not stop within 15s", "payload": {"node": nd.name, "stop_result": res}}


def ln_node_delete(node: Union[int, str], force: bool = False) -> Dict[str, Any]:
    cfg = load_config()
    nd = _node_dir(cfg, node)
    if not nd.exists():
        return {"ok": True, "payload": {"node": str(node), "deleted": False, "reason": "node dir missing"}}

    st = ln_node_status(node)
    running = bool(st.get("ok") and st.get("payload", {}).get("running") is True)
    if running and not force:
        return {"ok": False, "error": f"Refusing to delete running node: {nd}. Stop it first or pass force=true."}

    if running and force:
        ln_node_stop(node)

    try:
        shutil.rmtree(nd)
        return {"ok": True, "payload": {"node": nd.name, "deleted": True}}
    except Exception as e:
        return {"ok": False, "error": f"Failed to delete node dir: {e.__class__.__name__}: {e}", "payload": {"node": nd.name}}


# =============================================================================
# Lightning tools
# =============================================================================

def ln_getinfo(node: Union[int, str]) -> Dict[str, Any]:
    cfg = load_config()
    nd = _require_node_dir(cfg, node)
    gi = _run_json(_ln_base(cfg, nd) + ["getinfo"], cfg.cmd_timeout_s)
    if gi.get("ok"):
        return gi

    err = str(gi.get("error") or "")
    if _looks_like_node_not_running(err):
        return {"ok": True, "payload": {"node": nd.name, "running": False, "reason": err, "argv": gi.get("argv")}}
    return gi


def ln_listpeers(node: Union[int, str]) -> Dict[str, Any]:
    cfg = load_config()
    nd = _require_node_dir(cfg, node)
    return _run_json(_ln_base(cfg, nd) + ["listpeers"], cfg.cmd_timeout_s)


def ln_listfunds(node: Union[int, str]) -> Dict[str, Any]:
    cfg = load_config()
    nd = _require_node_dir(cfg, node)
    return _run_json(_ln_base(cfg, nd) + ["listfunds"], cfg.cmd_timeout_s)


def ln_listchannels(node: Union[int, str]) -> Dict[str, Any]:
    cfg = load_config()
    nd = _require_node_dir(cfg, node)
    return _run_json(_ln_base(cfg, nd) + ["listpeerchannels"], cfg.cmd_timeout_s)


def ln_newaddr(node: Union[int, str]) -> Dict[str, Any]:
    """
    Backward compatible: return original keys AND add payload.address convenience field.
    """
    cfg = load_config()
    nd = _require_node_dir(cfg, node)
    res = _run_json(_ln_base(cfg, nd) + ["newaddr"], cfg.cmd_timeout_s)
    if not res.get("ok"):
        return res

    payload = res.get("payload")
    if isinstance(payload, dict):
        address = payload.get("bech32") or payload.get("p2tr") or payload.get("p2sh-segwit") or payload.get("p2wpkh")
        if not address:
            for v in payload.values():
                if isinstance(v, str) and v:
                    address = v
                    break
        if address:
            payload["address"] = address
    return res


def ln_connect(from_node: Union[int, str], peer_id: str, host: str, port: int) -> Dict[str, Any]:
    cfg = load_config()
    nd = _require_node_dir(cfg, from_node)
    target = f"{peer_id}@{host}:{int(port)}"
    return _run_json(_ln_base(cfg, nd) + ["connect", target], cfg.cmd_timeout_s)


def ln_openchannel(from_node: Union[int, str], peer_id: str, amount_sat: int) -> Dict[str, Any]:
    cfg = load_config()
    nd = _require_node_dir(cfg, from_node)
    return _run_json(_ln_base(cfg, nd) + ["fundchannel", peer_id, str(int(amount_sat))], cfg.cmd_timeout_s)


def ln_invoice(node: Union[int, str], amount_msat: int, label: str, description: str) -> Dict[str, Any]:
    cfg = load_config()
    nd = _require_node_dir(cfg, node)
    return _run_json(_ln_base(cfg, nd) + ["invoice", str(int(amount_msat)), label, description], cfg.cmd_timeout_s)


def ln_pay(from_node: Union[int, str], bolt11: str) -> Dict[str, Any]:
    cfg = load_config()
    nd = _require_node_dir(cfg, from_node)
    return _run_json(_ln_base(cfg, nd) + ["-k", "pay", f"bolt11={bolt11}"], cfg.cmd_timeout_s)


def network_health() -> Dict[str, Any]:
    cfg = load_config()

    btc = btc_getblockchaininfo()
    bitcoin_ok = bool(btc.get("ok"))

    node_dirs = _list_node_dirs(cfg.lightning_base)
    nodes_out: List[Dict[str, Any]] = []
    running_count = 0

    for nd in node_dirs:
        st = ln_node_status(nd.name)
        if st.get("ok") and st.get("payload", {}).get("running") is True:
            running_count += 1
        nodes_out.append({"name": nd.name, "lightning_dir": str(nd), "status": st})

    if bitcoin_ok and running_count > 0:
        status = "ok" if running_count == len(node_dirs) else "degraded"
    elif bitcoin_ok:
        status = "degraded"
    else:
        status = "down"

    return {
        "status": status,
        "network": cfg.network,
        "runtime_dir": str(cfg.runtime_dir),
        "bitcoin_dir": str(cfg.bitcoin_dir),
        "lightning_base": str(cfg.lightning_base),
        "bitcoin": btc,
        "nodes": nodes_out,
        "summary": {"bitcoin_ok": bitcoin_ok, "nodes_total": len(node_dirs), "nodes_running": running_count},
    }


def _error(msg: str) -> Dict[str, Any]:
    return {"error": msg}


def handle(method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    params = params or {}

    try:
        if method == "list_tools":
            return list_tools()
        if method == "network_health":
            return network_health()

        # Bitcoin
        if method == "btc_getblockchaininfo":
            return btc_getblockchaininfo()
        if method == "btc_wallet_ensure":
            return btc_wallet_ensure(str(params["wallet_name"]))
        if method == "btc_getnewaddress":
            return btc_getnewaddress(params.get("wallet"))
        if method == "btc_sendtoaddress":
            return btc_sendtoaddress(
                str(params["address"]),
                str(params["amount_btc"]),
                wallet=params.get("wallet", "miner"),
            )
        if method == "btc_generatetoaddress":
            return btc_generatetoaddress(int(params["blocks"]), str(params["address"]))

        # Nodes
        if method == "ln_listnodes":
            return ln_listnodes()
        if method == "ln_node_create":
            return ln_node_create(params["node"])
        if method == "ln_node_status":
            return ln_node_status(params["node"])
        if method == "ln_node_start":
            return ln_node_start(params["node"])
        if method == "ln_node_stop":
            return ln_node_stop(params["node"])
        if method == "ln_node_delete":
            return ln_node_delete(params["node"], force=bool(params.get("force", False)))

        # Lightning
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
        if method == "ln_connect":
            return ln_connect(params["from_node"], str(params["peer_id"]), str(params["host"]), int(params["port"]))
        if method == "ln_openchannel":
            return ln_openchannel(params["from_node"], str(params["peer_id"]), int(params["amount_sat"]))
        if method == "ln_invoice":
            return ln_invoice(params["node"], int(params["amount_msat"]), str(params["label"]), str(params["description"]))
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