import time
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("demo-tools")

@mcp.tool()
def get_balance() -> dict:
    """Demo: pretend wallet balance (stub)."""
    return {"confirmed_sat": 250000, "unconfirmed_sat": 0}

@mcp.tool()
def decode_invoice(invoice: str) -> dict:
    """Demo: decode invoice (stub)."""
    return {
        "invoice_prefix": invoice[:12],
        "network": "regtest",
        "amount_sat": 1000,
        "memo": "demo-payment",
        "created_at": int(time.time())
    }

if __name__ == "__main__":
    mcp.run()
