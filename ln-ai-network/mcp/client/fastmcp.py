from __future__ import annotations

import atexit
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

    def __init__(self, _service_name: str = "ln-tools") -> None:
        self._id = 0

        self.process = subprocess.Popen(
            [sys.executable, "-u", "-m", "mcp.ln_mcp_server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        atexit.register(self.close)

    def call(self, method: str, **params: Any) -> Dict[str, Any]:
        self._id += 1

        request = {
            "id": self._id,
            "method": method,
            "params": params,
        }

        if not self.process.stdin or not self.process.stdout:
            raise RuntimeError("MCP pipes unavailable")

        self.process.stdin.write(json.dumps(request) + "\n")
        self.process.stdin.flush()

        # Detect early exit
        if self.process.poll() is not None:
            stderr = ""
            if self.process.stderr:
                stderr = self.process.stderr.read()
            raise RuntimeError(f"MCP process exited early: {stderr}")

        response_line = self.process.stdout.readline()
        if not response_line:
            stderr = ""
            if self.process.stderr:
                stderr = self.process.stderr.read()
            raise RuntimeError(f"No response from MCP server. STDERR: {stderr}")

        try:
            return json.loads(response_line)
        except json.JSONDecodeError as e:
            stderr = ""
            if self.process.stderr:
                try:
                    stderr = self.process.stderr.read()
                except Exception:
                    pass
            raise RuntimeError(
                f"MCP server returned non-JSON: {response_line.strip()[:120]!r}. "
                f"STDERR: {stderr[:300]}"
            ) from e

    def close(self) -> None:
        try:
            if self.process and self.process.poll() is None:
                self.process.terminate()
                self.process.wait(timeout=3)
        except Exception:
            try:
                if self.process:
                    self.process.kill()
            except Exception:
                pass
