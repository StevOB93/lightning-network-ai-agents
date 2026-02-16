import json
from typing import Any, Dict, List

from mcp.client.fastmcp import FastMCPClient

MCP_NAME = "ln-tools"


def build_observations(node_count: int) -> Dict[str, Any]:
    """
    Query MCP for network health and per-node balances.
    """

    client = FastMCPClient(MCP_NAME)

    try:
        # Network health
        health = client.call("network_health")

        observations: List[Dict[str, Any]] = []

        # Per-node funds
        for n in range(1, node_count + 1):
            lf = client.call("ln_listfunds", node=n)
            observations.append({
                "node": n,
                "funds": lf
            })

        return {
            "health": health,
            "observations": observations
        }

    finally:
        client.close()


def decide_intent(observations: Dict[str, Any]) -> Dict[str, Any]:
    """
    Simple deterministic intent logic.
    """

    if "error" in observations.get("health", {}):
        return {
            "intent": "noop",
            "reason": "Network health unavailable",
            "confidence": 0.1,
            "evidence": observations
        }

    return {
        "intent": "observe_only",
        "reason": "Network operational",
        "confidence": 0.9,
        "evidence": observations
    }


def main():
    try:
        node_count = 2  # For now deterministic; can parameterize later

        observations = build_observations(node_count)
        intent = decide_intent(observations)

        print(json.dumps(intent, indent=2))

    except Exception as e:
        print(json.dumps({
            "intent": "noop",
            "reason": f"Agent runtime failure: {str(e)}",
            "confidence": 0.1,
            "evidence": {
                "health": {
                    "error": "runtime_failure"
                },
                "observations": [
                    "exception"
                ]
            }
        }, indent=2))


if __name__ == "__main__":
    main()
