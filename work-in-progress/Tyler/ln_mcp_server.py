import json
import subprocess
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("ln-tools")

def _run_lightning_cli(args: list[str]) -> dict:
    proc = subprocess.run(
        ["lightning-cli", *args],
        capture_output=True,
        text=True,
        check=True
    )
    # lightning-cli prints JSON to stdout on success
    return json.loads(proc.stdout)

@mcp.tool()
def ln_getinfo() -> dict:
    """Read-only: Return basic Core Lightning node info (getinfo)."""
    return _run_lightning_cli(["getinfo"])

@mcp.tool()
def ln_listfunds() -> dict:
    """Read-only: Return wallet + channel funds (listfunds)."""
    return _run_lightning_cli(["listfunds"])

if __name__ == "__main__":
    mcp.run()
