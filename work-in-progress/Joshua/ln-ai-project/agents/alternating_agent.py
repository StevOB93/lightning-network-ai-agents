#!/usr/bin/env python3
"""
alternating_agent.py

This agent runs on EACH Core Lightning node.
Its job is to:
- Ensure exactly ONE unpaid invoice exists at any time
- Alternate payments automatically between nodes
- Optionally use AI to decide invoice parameters (amount, delay)

CRITICAL SAFETY RULE:
The agent NEVER decides whether to pay or invoice arbitrarily.
That decision is enforced by Lightning state (unpaid invoices).
"""

import sys
import time
import subprocess
import json
from datetime import datetime

# Optional AI layer.
# If ai_decider.py is missing or fails to load,
# the system still works with safe defaults.
try:
    from controllers.ai_decider import AIDecider
    AI_ENABLED = True
except ImportError:
    AI_ENABLED = False

# External binary used to talk to Core Lightning
LIGHTNING_CLI = "lightning-cli"

# Lightning network we are operating on
NETWORK = "regtest"

# How often the agent loops (seconds)
SLEEP_SECONDS = 5

# Fallback invoice amount if AI is disabled
DEFAULT_AMOUNT_MSAT = 10_000


def log(message: str):
    """
    Print timestamped log messages.
    These are redirected to log files by the startup script.
    """
    print(f"[{datetime.utcnow().isoformat()}] {message}", flush=True)


def run_cli(lightning_dir: str, args: list) -> dict:
    """
    Wrapper around lightning-cli.

    Why this exists:
    - Ensures the correct lightning-dir is always used
    - Forces regtest mode
    - Centralizes error handling

    If lightning-cli fails, we raise an exception so
    the agent logs the error instead of silently failing.
    """
    cmd = [
        LIGHTNING_CLI,
        f"--lightning-dir={lightning_dir}",
        f"--network={NETWORK}",
        *args,
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        # Surface Lightning errors clearly in logs
        raise RuntimeError(result.stderr.strip())

    return json.loads(result.stdout)


def get_unpaid_invoices(lightning_dir: str) -> list:
    """
    Query Core Lightning for all invoices and return ONLY unpaid ones.

    This is the KEY coordination mechanism:
    - If an unpaid invoice exists → someone must pay
    - If none exist → safe to create one
    """
    data = run_cli(lightning_dir, ["listinvoices"])
    return [
        inv for inv in data.get("invoices", [])
        if inv["status"] == "unpaid"
    ]


def create_invoice(lightning_dir: str, amount_msat: int):
    """
    Create a new invoice.

    IMPORTANT:
    - Labels MUST be unique forever in CLN
    - We use a timestamp to guarantee uniqueness
    """
    label = f"alt-{int(time.time())}"

    run_cli(
        lightning_dir,
        ["invoice", str(amount_msat), label, "alternating payment"],
    )

    log(f"Created invoice {label} ({amount_msat} msat)")


def pay_invoice(lightning_dir: str, bolt11: str):
    """
    Pay a BOLT11 invoice.

    If payment fails, Lightning will explain why
    (routing, liquidity, fees, etc.)
    """
    log("Paying invoice")
    run_cli(lightning_dir, ["pay", bolt11])
    log("Payment complete")


def main(lightning_dir: str):
    """
    Main agent loop.

    This function runs forever and enforces the invariant:
    EXACTLY ONE unpaid invoice exists at any time.
    """
    log(f"Alternating agent started for {lightning_dir}")

    # Initialize AI if available
    ai = AIDecider() if AI_ENABLED else None

    while True:
        try:
            unpaid = get_unpaid_invoices(lightning_dir)

            if unpaid:
                # There should only ever be ONE unpaid invoice globally
                # If we see one, our ONLY legal action is to pay it
                invoice = unpaid[0]
                pay_invoice(lightning_dir, invoice["bolt11"])

            else:
                # No unpaid invoices exist anywhere
                # Safe to create exactly one new invoice
                amount = (
                    ai.decide_invoice_amount({})
                    if ai else DEFAULT_AMOUNT_MSAT
                )
                create_invoice(lightning_dir, amount)

        except Exception as e:
            # Never crash the agent; log and retry
            log(f"ERROR: {e}")

        # Sleep prevents CPU spin and gives Lightning time to settle
        time.sleep(SLEEP_SECONDS)


if __name__ == "__main__":
    """
    Entry point.

    Usage:
    python3 alternating_agent.py <lightning-dir>
    """
    if len(sys.argv) != 2:
        print("Usage: python3 alternating_agent.py <lightning-dir>")
        sys.exit(1)

    main(sys.argv[1])
