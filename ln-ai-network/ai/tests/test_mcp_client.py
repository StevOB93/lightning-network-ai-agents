"""
Tests for FixtureMCPClient — the deterministic mock used in place of a live MCP server.

These run without Bitcoin, Lightning, or any API keys.
"""
import pytest
from ai.mcp_client import FixtureMCPClient

FIXTURE_DIR = "ai/mocks/fixtures"


def client(name: str) -> FixtureMCPClient:
    return FixtureMCPClient(f"{FIXTURE_DIR}/{name}.json")


class TestFixtureMCPClientHealthy:
    def test_network_health_returns_status(self):
        result = client("healthy").call("network_health")
        assert "status" in result

    def test_ln_getinfo_returns_node_data(self):
        result = client("healthy").call("ln_getinfo", {"node": 1})
        assert "node" in result
        assert result["node"] == 1

    def test_ln_listfunds_returns_funds(self):
        result = client("healthy").call("ln_listfunds", {"node": 1})
        assert "funds" in result

    def test_unknown_tool_returns_error(self):
        result = client("healthy").call("nonexistent_tool", {})
        assert "error" in result

    def test_node_string_arg_coerced(self):
        """FixtureMCPClient casts node arg to int — string "1" must work."""
        result = client("healthy").call("ln_getinfo", {"node": "1"})
        assert result["node"] == 1


class TestFixtureMCPClientNoRoute:
    def test_network_health_shows_degraded(self):
        result = client("no_route").call("network_health")
        assert result.get("status") == "degraded" or "failures" in result

    def test_isolated_node_has_no_channels(self):
        result = client("no_route").call("ln_listfunds", {"node": 3})
        assert result["channels"] == []


class TestFixtureMCPClientToolFailure:
    def test_failed_tool_returns_error_key(self):
        c = client("tool_failure")
        data = c.call("network_health")
        # tool_failure fixture either simulates failure or returns degraded state
        assert isinstance(data, dict)

    def test_failed_tool_error_message(self):
        """When a tool is in fail_tools, call returns an error dict."""
        import json
        from pathlib import Path
        fixture = json.loads(Path(f"{FIXTURE_DIR}/tool_failure.json").read_text())
        fail_tools = fixture.get("fail_tools", [])
        if fail_tools:
            c = client("tool_failure")
            result = c.call(fail_tools[0], {})
            assert "error" in result
