from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, Dict


###############################################################################
# ENVIRONMENT LOADER
###############################################################################


def _load_env() -> None:
    """
    Load environment variables from env.sh safely.

    Handles paths with spaces correctly by quoting the path.
    """

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(project_root, "env.sh")

    if not os.path.exists(env_path):
        raise RuntimeError(f"env.sh not found at: {env_path}")

    cmd = f'source "{env_path}" && env'

    proc = subprocess.run(
        ["bash", "-c", cmd],
        capture_output=True,
        text=True,
        check=True,
    )

    for line in proc.stdout.splitlines():
        key, _, value = line.partition("=")
        os.environ[key] = value


###############################################################################
# MCP CORE
###############################################################################


def _handle_network_health() -> Dict[str, Any]:
    """
    Basic deterministic network health check.
    """

    runtime_dir = os.environ.get("RUNTIME_DIR")
    lightning_base = os.environ.get("LIGHTNING_BASE")

    if not runtime_dir or not lightning_base:
        return {
            "status": "error",
            "reason": "environment_not_loaded",
        }

    return {
        "status": "ok",
        "runtime_dir": runtime_dir,
        "lightning_base": lightning_base,
    }


###############################################################################
# JSON-RPC LOOP
###############################################################################


def _run() -> None:
    """
    Deterministic stdin/stdout JSON RPC loop.
    """

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
            method = request.get("method")
            req_id = request.get("id")

            if method == "network_health":
                result = _handle_network_health()
            else:
                result = {"error": f"unknown_method: {method}"}

            response = {
                "id": req_id,
                "result": result,
            }

            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()

        except Exception as e:
            error_response = {
                "id": None,
                "error": str(e),
            }
            sys.stdout.write(json.dumps(error_response) + "\n")
            sys.stdout.flush()


###############################################################################
# ENTRYPOINT
###############################################################################


if __name__ == "__main__":
    _load_env()
    _run()
