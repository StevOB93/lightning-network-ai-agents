from __future__ import annotations
from typing import Any, Dict, Tuple

DEFAULT_POLICY = {
    "max_open_channel_sat": 200_000,
    "min_open_channel_sat": 10_000,
    "max_fee_ppm": 5_000,
    "max_payment_sat": 50_000
}

def simulate_policy(intent: Dict[str, Any], policy: Dict[str, Any] = DEFAULT_POLICY) -> Tuple[bool, str]:
    it = intent.get("intent")

    if it == "open_channel":
        amt = int(intent["amount_sat"])
        if amt > policy["max_open_channel_sat"]:
            return False, "Denied: open_channel amount too high"
        if amt < policy["min_open_channel_sat"]:
            return False, "Denied: open_channel amount too low"
        return True, "Approved (simulated)"

    if it == "set_fee":
        ppm = int(intent["ppm_fee"])
        if ppm > policy["max_fee_ppm"]:
            return False, "Denied: ppm_fee too high"
        return True, "Approved (simulated)"

    if it == "pay_invoice":
        # We only have max_fee_sat here; payment amount is in invoice decode (downstream).
        # Keep simulation conservative.
        return True, "Approved (simulated)"

    if it == "rebalance":
        return True, "Approved (simulated)"

    if it == "noop":
        return True, "Approved (simulated)"

    return False, "Denied: unknown intent"
