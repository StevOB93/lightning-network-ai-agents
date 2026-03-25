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

import atexit
import concurrent.futures
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Protocol


class MCPTimeoutError(Exception):
    """
    Raised when an MCP tool call does not complete within the configured deadline.

    This is distinct from a tool returning an error response — it means the MCP
    server itself became unresponsive and stopped communicating entirely. The
    executor treats this as a hard step failure; execution continues or aborts
    according to the step's on_error setting (same as any other tool failure).

    Why a separate exception type: callers that need to distinguish "tool said no"
    from "server went dark" can catch MCPTimeoutError specifically. The executor
    currently treats both the same way, but the distinction is useful in logs.

    Configuration:
      Set MCP_CALL_TIMEOUT_S in the environment to override the 30-second default.
      Increase for slow operations (channel opens can take several seconds on regtest);
      decrease for health checks where a fast response is expected.
    """


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
            node_raw = args.get("node")
            if node_raw is None:
                return {"error": "Missing required arg 'node'", "tool": tool}
            try:
                node_key = str(int(node_raw))
            except (ValueError, TypeError):
                return {"error": f"Invalid node value: {node_raw!r}", "tool": tool}
            return data["ln_getinfo"].get(node_key, {"error": f"No fixture for node {node_key}", "tool": tool})

        if tool == "ln_listfunds":
            node_raw = args.get("node")
            if node_raw is None:
                return {"error": "Missing required arg 'node'", "tool": tool}
            try:
                node_key = str(int(node_raw))
            except (ValueError, TypeError):
                return {"error": f"Invalid node value: {node_raw!r}", "tool": tool}
            return data["ln_listfunds"].get(node_key, {"error": f"No fixture for node {node_key}", "tool": tool})

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

    Timeout: each call runs inside a ThreadPoolExecutor future so that
    future.result(timeout=N) can enforce a hard deadline. Without this,
    a hung MCP server would block the pipeline thread forever — no result,
    no error, no user feedback. The background thread cannot be forcibly
    killed (Python limitation), but the pipeline moves on immediately when
    the deadline expires.
    """

    def __init__(self, fast_mcp_client: Any) -> None:
        self._client = fast_mcp_client

        # Threading lock: serialises all callers so only one MCP call is
        # ever in-flight at a time. FastMCPClient is not thread-safe, so
        # this is required regardless of EXECUTOR_MAX_WORKERS.
        self._lock = threading.Lock()

        # Timeout for each individual tool call, in seconds.
        # Read once at startup — changing it requires a restart.
        # Default: 30s covers slow regtest operations (channel opens, etc.)
        # without letting a truly hung server freeze the pipeline indefinitely.
        try:
            self._timeout_s = float(os.getenv("MCP_CALL_TIMEOUT_S") or "30")
        except (ValueError, TypeError):
            self._timeout_s = 30.0

        # Single-worker executor that runs the actual tool call in a background
        # thread. We submit the call to this executor and use future.result(timeout=N)
        # to apply the deadline. max_workers=1 is correct here — the _lock above
        # already ensures only one call enters at a time, so no concurrency is needed
        # inside the executor itself.
        self._call_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="mcp-call"
        )

        # Register a shutdown hook so the executor is always torn down on exit.
        # This prevents Python from hanging at interpreter shutdown waiting for
        # the background thread to finish — particularly important when a tool
        # call timed out and left a stuck thread in the pool.
        atexit.register(self.close)

    def close(self) -> None:
        """
        Shut down the background thread pool and close the underlying MCP client.

        Called automatically at process exit via atexit. Safe to call multiple
        times (both operations are idempotent).

        shutdown(wait=False): do NOT block waiting for an in-flight task.
        If a tool call timed out and the background thread is stuck waiting
        on the MCP server, waiting here would cause Python to hang on exit.
        Non-waiting shutdown lets the interpreter exit immediately; the stuck
        thread is a daemon and dies when the process does.
        """
        self._call_executor.shutdown(wait=False)
        # Delegate to the underlying client's cleanup (e.g. FastMCPClient
        # terminates its subprocess). Guard against double-close — atexit
        # may call this after the inner client's own atexit handler already ran.
        if hasattr(self._client, "close"):
            try:
                self._client.close()
            except Exception:
                pass

    def call(self, tool: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Execute a named MCP tool and return its result dict.

        Submits the call to a background thread (via _call_executor) and waits
        for the result with a timeout. This is the only way to apply a deadline
        to a blocking call in Python without asyncio.

        Raises:
          MCPTimeoutError — if the tool call does not return within _timeout_s seconds.
            The background thread continues running (Python cannot kill it), but the
            pipeline proceeds normally — the step is marked as failed.
          Any exception raised by the underlying client is propagated as-is.
        """
        args = args or {}
        with self._lock:
            # Submit the call to the background thread. The lock ensures only one
            # call is submitted at a time, matching the single-worker pool size.
            future = self._call_executor.submit(self._client.call, tool, **args)
            try:
                return future.result(timeout=self._timeout_s)
            except concurrent.futures.TimeoutError:
                # The MCP server did not respond within the configured window.
                # Raise MCPTimeoutError so the executor can log it as a step failure
                # with a clear, actionable error message for the operator.
                raise MCPTimeoutError(
                    f"MCP tool '{tool}' did not respond within {self._timeout_s}s. "
                    f"Check MCP server health or increase MCP_CALL_TIMEOUT_S "
                    f"(currently {self._timeout_s}s)."
                )
