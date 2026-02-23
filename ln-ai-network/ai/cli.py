from __future__ import annotations

import argparse
import json
from typing import Any, Dict

from ai.command_queue import enqueue, last_outbox


def main() -> None:
    p = argparse.ArgumentParser(prog="ai.cli", description="ln-ai-network agent CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    health = sub.add_parser("health", help="Ask agent to check network health and report back (no LLM)")
    health.add_argument("--raw", action="store_true", help="Include raw network_health JSON in the report")

    ask_p = sub.add_parser("ask", help="Send an arbitrary user request to the agent (may use LLM)")
    ask_p.add_argument("text", help="User request text, quoted")

    sub.add_parser("last", help="Print the last agent response from outbox")

    args = p.parse_args()

    if args.cmd == "health":
        msg = enqueue(
            "Check the health of the regtest Lightning network and report status clearly.",
            meta={"kind": "health_check", "include_raw": bool(args.raw)},
        )
        print("[queued] health_check")
        print(json.dumps(msg, indent=2))

    elif args.cmd == "ask":
        msg = enqueue(str(args.text), meta={"kind": "freeform"})
        print("[queued] freeform")
        print(json.dumps(msg, indent=2))

    elif args.cmd == "last":
        out = last_outbox()
        if not out:
            print("No outbox messages yet.")
            return
        print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()