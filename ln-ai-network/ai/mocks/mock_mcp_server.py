import json
import os
from pathlib import Path
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("mock-ln-tools")

FIXTURE = os.environ.get("LN_FIXTURE", "healthy.json")
FIXTURE_PATH = Path(__file__).parent / "fixtures" / FIXTURE

def _load() -> dict:
    return json.loads(FIXTURE_PATH.read_text())

@mcp.tool()
def network_health() -> dict:
    """Mock-only: canonical health oracle fixture (isolated from real regtest)."""
    data = _load()
    if data.get("simulate_tool_failure") and "network_health" in data.get("fail_tools", []):
        return {"error": "ToolFailure: network_health"}
    return data["network_health"]

@mcp.tool()
def ln_getinfo(node: int) -> dict:
    """Mock-only: node info fixture."""
    data = _load()
    if data.get("simulate_tool_failure") and "ln_getinfo" in data.get("fail_tools", []):
        return {"error": "ToolFailure: ln_getinfo", "node": node}
    return data["ln_getinfo"][str(node)]

@mcp.tool()
def ln_listfunds(node: int) -> dict:
    """Mock-only: listfunds fixture."""
    data = _load()
    if data.get("simulate_tool_failure") and "ln_listfunds" in data.get("fail_tools", []):
        return {"error": "ToolFailure: ln_listfunds", "node": node}
    return data["ln_listfunds"][str(node)]

if __name__ == "__main__":
    mcp.run()
