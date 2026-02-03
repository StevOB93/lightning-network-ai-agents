"""
ai_decider.py

This module represents the "AI" in the system.

IMPORTANT DESIGN DECISION:
- AI is NOT allowed to decide whether to pay or invoice
- AI ONLY decides parameters (amount, delay, etc.)

This prevents:
- Deadlocks
- Unsafe financial behavior
- Non-deterministic failures
"""

class AIDecider:
    def decide_invoice_amount(self, node_info: dict) -> int:
        """
        Decide how large the next invoice should be (in millisatoshis).

        node_info is reserved for future use (liquidity, balance, history).

        Safe default:
        - Always return a small, routable amount
        """
        return 10_000
