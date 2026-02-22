from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Protocol, Optional


class MCPClient(Protocol):
    """
    Minimal boundary interface: all execution MUST go through this.
    """
    def call(self, tool: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]: ...


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


class FastMCPClientWrapper:
    """
    Wraps the project's FastMCPClient (kwargs-based) behind the dict-based MCPClient protocol.
    Keeps agent core deterministic and stable even if FastMCPClient signature changes.
    """

    def __init__(self, fast_mcp_client: Any):
        self._client = fast_mcp_client

    def call(self, tool: str, args: Dict[str, Any] | None = None) -> Dict[str, Any]:
        args = args or {}
        # FastMCPClient currently expects kwargs; adapt here.
        return self._client.call(tool, **args)