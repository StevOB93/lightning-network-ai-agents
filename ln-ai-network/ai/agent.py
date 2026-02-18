from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Tuple

from ai.mcp_client import FixtureMCPClient
from ai.intent_validate import validate_intent_safety
from ai.policy_sim import simulate_policy
from ai.prompt import build_intent_prompt
from ai.llm_client import LLMClient


def _safe_tool_call(
    client: FixtureMCPClient,
    tool: str,
    args: Dict[str, Any] | None = None
) -> Tuple[bool, Dict[str, Any]]:
    res = client.call(tool, args)
    if "error" in res:
        return False, res
    return True, res


def derive_observations(
    health: Dict[str, Any],
    funds_by_node: Dict[int, Dict[str, Any]]
) -> List[str]:
    obs: List[str] = []
    psr = health.get("payment_success_rate")
    if psr is not None:
        obs.append(f"payment_success_rate={psr:.2f}")

    failures = health.get("failures", {})
    if failures:
        obs.append(f"failures={failures}")

    # Inbound liquidity heuristic: inbound is remote_sat
    for node, lf in funds_by_node.items():
        channels = lf.get("channels", [])
        inbound_total = sum(int(ch.get("remote_sat", 0)) for ch in channels)
        if len(channels) == 0:
            obs.append(f"node{node}: has 0 channels")
        elif inbound_total < 20_000:
            obs.append(f"node{node}: low inbound_liquidity (remote_sat_total={inbound_total})")

    return obs[:20]


def choose_intent(
    health: Dict[str, Any],
    funds_by_node: Dict[int, Dict[str, Any]],
    node_count: int
) -> Dict[str, Any]:
    failures = health.get("failures", {})
    no_route = int(failures.get("no_route", 0))
    liq = int(failures.get("insufficient_liquidity", 0))
    psr = float(health.get("payment_success_rate", 1.0))

    degrees = {n: len(funds_by_node[n].get("channels", [])) for n in funds_by_node}
    lowest_degree_node = sorted(degrees.items(), key=lambda x: (x[1], x[0]))[0][0]

    def inbound(n: int) -> int:
        chs = funds_by_node[n].get("channels", [])
        return sum(int(ch.get("remote_sat", 0)) for ch in chs)

    lowest_inbound_node = sorted(
        [(n, inbound(n)) for n in funds_by_node],
        key=lambda x: (x[1], x[0])
    )[0][0]

    if no_route > 0 or psr < 0.85:
        from_node = 1
        to_node = lowest_degree_node if lowest_degree_node != 1 else (2 if node_count >= 2 else 1)
        if from_node != to_node:
            return {
                "intent": "open_channel",
                "from_node": from_node,
                "to_node": to_node,
                "amount_sat": 100000,
                "reason": f"Improve connectivity (no_route={no_route}, psr={psr:.2f})"
            }

    if liq > 0:
        from_node = 1
        to_node = lowest_inbound_node if lowest_inbound_node != 1 else (2 if node_count >= 2 else 1)
        if from_node != to_node:
            return {
                "intent": "open_channel",
                "from_node": from_node,
                "to_node": to_node,
                "amount_sat": 100000,
                "reason": f"Address liquidity failures (insufficient_liquidity={liq})"
            }

    return {
        "intent": "noop",
        "reason": "Network healthy or insufficient evidence to propose action"
    }


def run_agent(fixture_path: str, node_count: int) -> Dict[str, Any]:
    client = FixtureMCPClient(fixture_path)

    ok, health = _safe_tool_call(client, "network_health")
    if not ok:
        return {
            "intent": "noop",
            "reason": f"Cannot read network_health: {health.get('error')}",
            "confidence": 0.2,
            "evidence": {"health": {"error": health.get("error")}, "observations": ["tool_failure(network_health)"]}
        }

    funds_by_node: Dict[int, Dict[str, Any]] = {}
    for n in range(1, node_count + 1):
        ok, lf = _safe_tool_call(client, "ln_listfunds", {"node": n})
        if not ok:
            return {
                "intent": "noop",
                "reason": f"Cannot read ln_listfunds(node={n}): {lf.get('error')}",
                "confidence": 0.2,
                "evidence": {"health": health, "observations": [f"tool_failure(ln_listfunds,node={n})"]}
            }
        funds_by_node[n] = lf

    observations = derive_observations(health, funds_by_node)

    # --- choose core_intent via LLM (optional) or heuristic fallback ---
    use_llm = os.environ.get("USE_LLM", "0") == "1"
    if use_llm:
        state_summary = {
            "health": health,
            "observations": observations,
            "nodes": {
                str(n): {
                    "channels": len(funds_by_node[n].get("channels", [])),
                    "confirmed_sat": funds_by_node[n].get("funds", {}).get("confirmed_sat", None)
                }
                for n in funds_by_node
            }
        }
        try:
            llm = LLMClient()
            core_intent = llm.propose_intent(build_intent_prompt(state_summary))
        except Exception as e:
            core_intent = {"intent": "noop", "reason": f"LLM unavailable: {e}"}
    else:
        core_intent = choose_intent(health, funds_by_node, node_count)

    # --- wrap into full intent envelope (evidence, confidence) ---
    intent = {
        **core_intent,
        "confidence": 0.7 if core_intent.get("intent") != "noop" else 0.5,
        "evidence": {"health": health, "observations": observations}
    }

    # Safety validation (Person A guardrails)
    safe_ok, safe_msg = validate_intent_safety(intent)
    if not safe_ok:
        return {
            "intent": "noop",
            "reason": f"Intent rejected by safety guard: {safe_msg}",
            "confidence": 0.2,
            "evidence": {"health": health, "observations": ["safety_guard_rejected"]}
        }

    # Policy simulation (informational only)
    approved, policy_msg = simulate_policy(intent)
    intent["policy_sim"] = {"approved": approved, "reason": policy_msg}

    return intent


if __name__ == "__main__":
    out = run_agent("ai/mocks/fixtures/no_route.json", node_count=3)
    print(json.dumps(out, indent=2))
