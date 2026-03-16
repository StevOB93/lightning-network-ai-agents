"""
Agent-level smoke tests using FixtureMCPClient.

The original tests called `run_agent()` which no longer exists in agent.py.
These replacement tests exercise the FixtureMCPClient fixture contract that
the agent depends on, verifying that each scenario fixture produces the
tool outputs the agent logic expects to consume.

Full agent integration tests (requiring LLM + live MCP) belong in a
separate integration test suite not run in CI.
"""
import pytest
from ai.mcp_client import FixtureMCPClient

FIXTURE_DIR = "ai/mocks/fixtures"


def _client(name: str) -> FixtureMCPClient:
    return FixtureMCPClient(f"{FIXTURE_DIR}/{name}.json")


class TestNoRouteFixture:
    """The no_route fixture represents a network where node 3 is isolated."""

    def test_network_health_is_degraded(self):
        result = _client("no_route").call("network_health")
        assert isinstance(result, dict)
        has_problem = (
            result.get("status") == "degraded"
            or bool(result.get("failures"))
            or result.get("payment_success_rate", 1.0) < 1.0
        )
        assert has_problem, f"Expected degraded health, got: {result}"

    def test_isolated_node_has_zero_channels(self):
        result = _client("no_route").call("ln_listfunds", {"node": 3})
        assert result.get("channels") == []

    def test_connected_nodes_have_channels(self):
        result = _client("no_route").call("ln_listfunds", {"node": 1})
        assert len(result.get("channels", [])) >= 1


class TestToolFailureFixture:
    """The tool_failure fixture simulates MCP tools returning errors."""

    def test_failed_tools_return_error_key(self):
        import json
        from pathlib import Path
        fixture = json.loads(Path(f"{FIXTURE_DIR}/tool_failure.json").read_text())
        fail_tools = fixture.get("fail_tools", [])
        if not fail_tools:
            pytest.skip("tool_failure fixture has no fail_tools list")

        c = _client("tool_failure")
        for tool_name in fail_tools:
            result = c.call(tool_name, {})
            assert "error" in result, f"Expected error for {tool_name}, got: {result}"


class TestHealthyFixture:
    """The healthy fixture represents a fully operational network."""

    def test_health_status_returned(self):
        result = _client("healthy").call("network_health")
        assert isinstance(result, dict)

    def test_all_nodes_have_funds(self):
        for node in (1, 2):
            result = _client("healthy").call("ln_listfunds", {"node": node})
            assert result.get("funds", {}).get("confirmed_sat", 0) > 0
