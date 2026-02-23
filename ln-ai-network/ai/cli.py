from __future__ import annotations

import argparse
import json
from ai.command_queue import enqueue, last_outbox


def main() -> None:
    p = argparse.ArgumentParser(prog="ai.cli", description="ln-ai-network agent CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    # Cheap / deterministic
    health = sub.add_parser("health", help="Network health report (NO LLM)")
    health.add_argument("--raw", action="store_true", help="Include raw JSON in outbox entry")

    btc = sub.add_parser("btc", help="Bitcoin operations (NO LLM)")
    btc_sub = btc.add_subparsers(dest="btc_cmd", required=True)
    btc_sub.add_parser("info", help="getblockchaininfo")
    send = btc_sub.add_parser("send", help="sendtoaddress")
    send.add_argument("--address", required=True)
    send.add_argument("--amount-btc", required=True)
    mine = btc_sub.add_parser("mine", help="generatetoaddress")
    mine.add_argument("--blocks", required=True, type=int)
    mine.add_argument("--address", required=True)

    ln = sub.add_parser("ln", help="Lightning operations (NO LLM)")
    ln_sub = ln.add_subparsers(dest="ln_cmd", required=True)

    info = ln_sub.add_parser("info", help="getinfo")
    info.add_argument("--node", required=True, type=int)

    peers = ln_sub.add_parser("peers", help="listpeers")
    peers.add_argument("--node", required=True, type=int)

    funds = ln_sub.add_parser("funds", help="listfunds")
    funds.add_argument("--node", required=True, type=int)

    chans = ln_sub.add_parser("channels", help="listpeerchannels")
    chans.add_argument("--node", required=True, type=int)

    newaddr = ln_sub.add_parser("newaddr", help="newaddr")
    newaddr.add_argument("--node", required=True, type=int)

    connect = ln_sub.add_parser("connect", help="connect from one node to another (agent resolves peer id/port)")
    connect.add_argument("--from-node", required=True, type=int)
    connect.add_argument("--to-node", required=True, type=int)

    opench = ln_sub.add_parser("openchannel", help="open channel (agent resolves peer id; will connect first)")
    opench.add_argument("--from-node", required=True, type=int)
    opench.add_argument("--to-node", required=True, type=int)
    opench.add_argument("--amount-sat", required=True, type=int)

    invoice = ln_sub.add_parser("invoice", help="create invoice")
    invoice.add_argument("--node", required=True, type=int)
    invoice.add_argument("--amount-msat", type=int, default=None)
    invoice.add_argument("--label", default=None)
    invoice.add_argument("--description", default="invoice")

    pay = ln_sub.add_parser("pay", help="pay invoice")
    pay.add_argument("--from-node", required=True, type=int)
    pay.add_argument("--bolt11", required=True)

    # Deliberate LLM
    ask = sub.add_parser("ask", help="Freeform request (USES LLM ONLY IF --llm)")
    ask.add_argument("--llm", action="store_true", help="Explicitly allow LLM spend for this request")
    ask.add_argument("text")

    sub.add_parser("last", help="Print last outbox entry")

    args = p.parse_args()

    if args.cmd == "health":
        msg = enqueue(
            "health_check",
            meta={"kind": "health_check", "include_raw": bool(args.raw)},
        )
        print(json.dumps({"queued": "health_check", "msg": msg}, indent=2))
        return

    if args.cmd == "btc":
        if args.btc_cmd == "info":
            msg = enqueue("btc_info", meta={"kind": "btc_info"})
        elif args.btc_cmd == "send":
            msg = enqueue(
                "btc_send",
                meta={"kind": "btc_send", "address": args.address, "amount_btc": args.amount_btc},
            )
        elif args.btc_cmd == "mine":
            msg = enqueue(
                "btc_mine",
                meta={"kind": "btc_mine", "blocks": args.blocks, "address": args.address},
            )
        else:
            raise SystemExit("unknown btc command")

        print(json.dumps({"queued": f"btc.{args.btc_cmd}", "msg": msg}, indent=2))
        return

    if args.cmd == "ln":
        if args.ln_cmd == "info":
            msg = enqueue("ln_getinfo", meta={"kind": "ln_getinfo", "node": args.node})
        elif args.ln_cmd == "peers":
            msg = enqueue("ln_listpeers", meta={"kind": "ln_listpeers", "node": args.node})
        elif args.ln_cmd == "funds":
            msg = enqueue("ln_listfunds", meta={"kind": "ln_listfunds", "node": args.node})
        elif args.ln_cmd == "channels":
            msg = enqueue("ln_listchannels", meta={"kind": "ln_listchannels", "node": args.node})
        elif args.ln_cmd == "newaddr":
            msg = enqueue("ln_newaddr", meta={"kind": "ln_newaddr", "node": args.node})
        elif args.ln_cmd == "connect":
            msg = enqueue(
                "ln_connect",
                meta={"kind": "ln_connect", "from_node": args.from_node, "to_node": args.to_node},
            )
        elif args.ln_cmd == "openchannel":
            msg = enqueue(
                "ln_openchannel",
                meta={
                    "kind": "ln_openchannel",
                    "from_node": args.from_node,
                    "to_node": args.to_node,
                    "amount_sat": args.amount_sat,
                },
            )
        elif args.ln_cmd == "invoice":
            msg = enqueue(
                "ln_invoice",
                meta={
                    "kind": "ln_invoice",
                    "node": args.node,
                    "amount_msat": args.amount_msat,
                    "label": args.label,  # agent will generate if None
                    "description": args.description,
                },
            )
        elif args.ln_cmd == "pay":
            msg = enqueue(
                "ln_pay",
                meta={"kind": "ln_pay", "from_node": args.from_node, "bolt11": args.bolt11},
            )
        else:
            raise SystemExit("unknown ln command")

        print(json.dumps({"queued": f"ln.{args.ln_cmd}", "msg": msg}, indent=2))
        return

    if args.cmd == "ask":
        # Deliberate spend: require --llm
        if not args.llm:
            raise SystemExit("Refusing to use LLM without --llm (to avoid spending credits).")
        msg = enqueue(
            args.text,
            meta={"kind": "freeform", "use_llm": True},
        )
        print(json.dumps({"queued": "ask (LLM)", "msg": msg}, indent=2))
        return

    if args.cmd == "last":
        out = last_outbox()
        if not out:
            print("No outbox messages yet.")
            return
        print(json.dumps(out, indent=2))
        return


if __name__ == "__main__":
    main()