from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Protocol


class MCPClient(Protocol):
    """
    Minimal boundary interface: all execution MUST go through this.
    This keeps the agent stable even if the underlying MCP client changes.
    """

    def call(self, tool: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        ...


class FixtureMCPClient:
    """
    Deterministic mock MCP client.
    Reads from a fixture file to emulate MCP tool outputs.
    """

    def __init__(self, fixture_path: str) -> None:
        self.fixture_path = Path(fixture_path)

    def call(self, tool: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        args = args or {}
        data = json.loads(self.fixture_path.read_text(encoding="utf-8"))

        if data.get("simulate_tool_failure") and tool in data.get("fail_tools", []):
            return {"error": f"ToolFailure: {tool}", "tool": tool}

        # Extend as needed; keep deterministic behavior.
        if tool == "network_health":
            return data["network_health"]

        if tool == "ln_getinfo":
            node = int(args["node"])
            return data["ln_getinfo"][str(node)]

        if tool == "ln_listfunds":
            node = int(args["node"])
            return data["ln_listfunds"][str(node)]

        return {"error": f"Unknown tool '{tool}'", "tool": tool, "args": args}


class FastMCPClientWrapper:
    """
    Wraps a kwargs-based MCP client behind the dict-based MCPClient protocol.
    """

    def __init__(self, fast_mcp_client: Any) -> None:
        self._client = fast_mcp_client

    def call(self, tool: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        args = args or {}
        return self._client.call(tool, **args)