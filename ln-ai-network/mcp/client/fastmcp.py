from __future__ import annotations

import json
import subprocess
import sys
from typing import Any, Dict


class FastMCPClient:
    """
    Deterministic MCP client.

    - Always launches the ln_mcp_server module
    - Uses unbuffered mode
    - Communicates over stdin/stdout JSON
    """

    def __init__(self, _service_name: str = "ln-tools"):
        self._id = 0

        self.process = subprocess.Popen(
            [sys.executable, "-u", "-m", "mcp.ln_mcp_server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def call(self, method: str, **params: Any) -> Dict[str, Any]:
        self._id += 1

        request = {
            "id": self._id,
            "method": method,
            "params": params,
        }

        if not self.process.stdin or not self.process.stdout:
            raise RuntimeError("MCP pipes unavailable")

        # Send request
        self.process.stdin.write(json.dumps(request) + "\n")
        self.process.stdin.flush()

        # Detect early exit
        if self.process.poll() is not None:
            stderr = self.process.stderr.read()
            raise RuntimeError(f"MCP process exited early: {stderr}")

        # Read response
        response_line = self.process.stdout.readline()

        if not response_line:
            stderr = self.process.stderr.read()
            raise RuntimeError(f"No response from MCP server. STDERR: {stderr}")

        return json.loads(response_line)

    def close(self):
        if self.process:
            self.process.terminate()
            self.process.wait()
