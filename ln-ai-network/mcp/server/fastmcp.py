from __future__ import annotations

import json
import sys
import time
from typing import Callable, Dict


class FastMCP:
    """
    Deterministic MCP daemon implementation.

    - Registers tools via decorator
    - Runs forever even if stdin is closed
    - Supports JSON-RPC if input is provided
    """

    def __init__(self, name: str):
        self.name = name
        self._tools: Dict[str, Callable] = {}

    def tool(self):
        def decorator(func: Callable):
            self._tools[func.__name__] = func
            return func
        return decorator

    def _handle_request(self, line: str):
        try:
            request = json.loads(line.strip())
            method = request.get("method")
            params = request.get("params", {})
            request_id = request.get("id")

            if method not in self._tools:
                return {"id": request_id, "error": f"Unknown method: {method}"}

            result = self._tools[method](**params)
            return {"id": request_id, "result": result}

        except Exception as e:
            return {"id": None, "error": str(e)}

    def run(self):
        print(f"[FastMCP] {self.name} started.")
        sys.stdout.flush()

        # Run forever
        while True:
            try:
                # Try to read without blocking forever on closed stdin
                if sys.stdin and not sys.stdin.closed:
                    line = sys.stdin.readline()
                    if line:
                        response = self._handle_request(line)
                        sys.stdout.write(json.dumps(response) + "\n")
                        sys.stdout.flush()

                time.sleep(0.5)

            except Exception:
                time.sleep(0.5)
