from __future__ import annotations

import json
import time
import traceback
from typing import Any, Dict

from mcp.client.fastmcp import FastMCPClient


###############################################################################
# CONFIGURATION
###############################################################################

POLL_INTERVAL_SECONDS = 5
MCP_NAME = "ln-tools"


###############################################################################
# AGENT CORE
###############################################################################


class LightningAgent:
    def __init__(self) -> None:
        self.mcp = FastMCPClient(MCP_NAME)

    def observe(self) -> Dict[str, Any]:
        """
        Collect network state via MCP.
        """

        health = self.mcp.call("network_health")

        observations = {
            "health": health,
        }

        return observations

    def decide(self, observations: Dict[str, Any]) -> Dict[str, Any]:
        """
        Decide next action based on observations.
        """

        result = observations.get("health", {}).get("result", {})

        if result.get("status") == "ok":
            return {
                "intent": "observe_only",
                "reason": "Network operational",
                "confidence": 0.9,
            }

        return {
            "intent": "investigate",
            "reason": "Network unhealthy",
            "confidence": 0.5,
        }

    def act(self, decision: Dict[str, Any]) -> None:
        """
        Execute actions if needed.
        """

        if decision["intent"] == "observe_only":
            return

        # Future: call MCP methods to rebalance, open channels, etc.

    def run(self) -> None:
        """
        Persistent control loop.
        """

        print("[AGENT] Persistent control loop started.")

        while True:
            try:
                observations = self.observe()
                decision = self.decide(observations)
                self.act(decision)

                print(json.dumps({
                    "decision": decision,
                    "observations": observations
                }, indent=2))

                time.sleep(POLL_INTERVAL_SECONDS)

            except KeyboardInterrupt:
                print("[AGENT] Shutdown requested.")
                break

            except Exception:
                print("[AGENT] Runtime error:")
                traceback.print_exc()
                time.sleep(POLL_INTERVAL_SECONDS)


###############################################################################
# ENTRYPOINT
###############################################################################


if __name__ == "__main__":
    agent = LightningAgent()
    agent.run()
