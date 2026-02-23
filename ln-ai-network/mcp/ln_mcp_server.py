from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict

from mcp.server.fastmcp import FastMCP


###############################################################################
# MCP SERVER — Lightning Read-Only Interface
#
# - Deterministic
# - Regtest only
# - Multi-node aware
# - Uses env.sh for absolute paths
# - No execution capabilities
###############################################################################

mcp = FastMCP("ln-tools")

###############################################################################
# Environment Loading
###############################################################################

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
ENV_PATH = PROJECT_ROOT / "env.sh"


def _load_env() -> None:
    """
    Load env.sh into this Python process deterministically.
    """
    proc = subprocess.run(
        ["bash", "-c", f"source {ENV_PATH} && env"],
        capture_output=True,
        text=True,
        check=True,
    )

    for line in proc.stdout.splitlines():
        key, _, value = line.partition("=")
        os.environ[key] = value


_load_env()

LN_RUNTIME = os.environ.get("LN_RUNTIME")
if not LN_RUNTIME:
    raise RuntimeError("LN_RUNTIME not found in env.sh")


###############################################################################
# Utility Helpers
###############################################################################

def _lightning_dir(node: int) -> str:
    return f"{LN_RUNTIME}/lightning/node-{node}"


def _run_lightning_cli(node: int, args: list[str]) -> Dict[str, Any]:
    """
    Run lightning-cli in regtest mode against a specific node.
    """
    ln_dir = _lightning_dir(node)

    cmd = [
        "lightning-cli",
        "--network=regtest",
        f"--lightning-dir={ln_dir}",
        *args,
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(proc.stdout)

    except subprocess.CalledProcessError as e:
        return {
            "error": "lightning_cli_failed",
            "stderr": e.stderr.strip(),
            "stdout": e.stdout.strip(),
            "node": node,
            "args": args,
        }

    except json.JSONDecodeError:
        return {
            "error": "invalid_json_from_lightning",
            "node": node,
            "args": args,
        }


def _run_network_test() -> Dict[str, Any]:
    """
    Wrap network_test.sh as structured health endpoint.
    """
    script = PROJECT_ROOT / "scripts" / "network_test.sh"

    try:
        proc = subprocess.run(
            [str(script), "2"],  # adjust node count if needed later
            capture_output=True,
            text=True,
            check=False,
        )

        return {
            "healthy": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
        }

    except Exception as e:
        return {
            "healthy": False,
            "exit_code": -1,
            "error": str(e),
        }


###############################################################################
# MCP TOOLS (READ-ONLY)
###############################################################################

@mcp.tool()
def network_health() -> Dict[str, Any]:
    """
    Authoritative network health check via network_test.sh.
    """
    return _run_network_test()


@mcp.tool()
def ln_getinfo(node: int = 1) -> Dict[str, Any]:
    """
    Read-only: Return Core Lightning getinfo for specific node.
    """
    return _run_lightning_cli(node, ["getinfo"])


@mcp.tool()
def ln_listfunds(node: int = 1) -> Dict[str, Any]:
    """
    Read-only: Return wallet + channel funds for specific node.
    """
    return _run_lightning_cli(node, ["listfunds"])


@mcp.tool()
def ln_decodepay(node: int, bolt11: str) -> Dict[str, Any]:
    """
    Read-only: Decode invoice (uses node context for consistency).
    """
    return _run_lightning_cli(node, ["decodepay", bolt11])


###############################################################################
# Server Entrypoint
###############################################################################

if __name__ == "__main__":
    mcp.run()
