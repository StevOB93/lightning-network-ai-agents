from __future__ import annotations

# =============================================================================
# mcp_client — MCP tool execution boundary
#
# All tool calls in the agent and pipeline stages MUST go through the MCPClient
# protocol. This boundary keeps the execution layer swappable:
#   - Production: FastMCPClientWrapper → real Lightning/Bitcoin network
#   - Tests:      FixtureMCPClient     → deterministic JSON fixture
#
# The MCPClient protocol uses structural subtyping (Protocol class), so any
# object that implements call(tool, args) is accepted without inheriting from
# a base class. This avoids import coupling between the agent and any specific
# MCP implementation.
#
# Wire format (both implementations):
#   Input:  tool name (str) + args dict
#   Output: dict — always a dict, even on error
#     Success: {"result": {"ok": True, "payload": {...}}}
#     Error:   {"error": "message"} or {"result": {"ok": False, "error": "..."}}
#   _is_tool_error() in ai.tools handles all error shape variants.
# =============================================================================

import json
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Protocol


class MCPClient(Protocol):
    """
    Minimal protocol interface for MCP tool execution.

    All agent and pipeline code calls this interface, never a concrete class
    directly. Swapping the backend (real vs. fixture) only requires changing
    the object passed at construction time.
    """

    def call(self, tool: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Execute a named MCP tool with the given args dict.
        Must always return a dict (never raise on tool errors — encode them in the dict).
        """
        ...


class FixtureMCPClient:
    """
    Deterministic mock MCP client for use in unit and integration tests.

    Reads tool responses from a JSON fixture file at the path given to __init__.
    Supports simulated tool failure for specific tools via fixture flags:
      {"simulate_tool_failure": true, "fail_tools": ["ln_getinfo", ...]}

    The fixture format mirrors the real MCP response shape so tests exercise
    the same _is_tool_error() parsing paths that production code does.

    To extend: add a new `if tool == "..."` branch, keyed to whatever the
    fixture JSON contains.
    """

    def __init__(self, fixture_path: str) -> None:
        self.fixture_path = Path(fixture_path)

    def call(self, tool: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        args = args or {}
        data = json.loads(self.fixture_path.read_text(encoding="utf-8"))

        # Fixture-level failure injection: return an error dict for any tool in fail_tools
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

        # Unknown tool: return an error dict in the standard MCP error shape
        return {"error": f"Unknown tool '{tool}'", "tool": tool, "args": args}


class FastMCPClientWrapper:
    """
    Adapts a kwargs-based MCP client (FastMCPClient) to the dict-based MCPClient protocol.

    FastMCPClient's call() signature uses **kwargs for tool arguments:
      client.call("ln_getinfo", node=1)

    Our MCPClient protocol uses a single args dict:
      client.call("ln_getinfo", args={"node": 1})

    This wrapper bridges the two by unpacking the args dict as kwargs before
    passing through to the underlying client.

    Thread safety: a per-instance Lock serialises concurrent calls through the
    underlying FastMCPClient, which uses a single connection and is not designed
    for concurrent access. This makes FastMCPClientWrapper safe to use with
    EXECUTOR_MAX_WORKERS > 1, at the cost of serialising all tool calls (i.e.
    parallel plan steps wait on each other at the MCP boundary). For true
    parallelism, replace this with a connection-pooled client.
    """

    def __init__(self, fast_mcp_client: Any) -> None:
        self._client = fast_mcp_client
        self._lock = threading.Lock()

    def call(self, tool: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        args = args or {}
        with self._lock:
            # Spread the args dict as keyword arguments to match FastMCPClient's signature
            return self._client.call(tool, **args)
