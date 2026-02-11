from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Dict

class FixtureMCPClient:
    """
    Person A mock MCP client.
    Reads from a single fixture file to emulate MCP tool outputs deterministically.
    """
    def __init__(self, fixture_path: str):
        self.fixture_path = Path(fixture_path)

    def call(self, tool: str, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
        args = args or {}
        data = json.loads(self.fixture_path.read_text())

        # Tool failure simulation (meaningful signal)
        if data.get("simulate_tool_failure") and tool in data.get("fail_tools", []):
            return {"error": f"ToolFailure: {tool}", "tool": tool}

        if tool == "network_health":
            return data["network_health"]

        if tool == "ln_getinfo":
            node = int(args["node"])
            return data["ln_getinfo"][str(node)]

        if tool == "ln_listfunds":
            node = int(args["node"])
            return data["ln_listfunds"][str(node)]

        return {"error": f"Unknown tool '{tool}'"}
