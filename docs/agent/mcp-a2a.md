---
title: MCP & A2A Overview
---

# MCP & A2A Overview

## MCP — Model Context Protocol

MCP (Model Context Protocol) is the interface between the AI agent and the Bitcoin/Lightning infrastructure. The agent calls named tools; the MCP server translates those calls into `bitcoin-cli` / `lightning-cli` subprocess invocations.

### How it's used in this project

```
AI Agent (ai/pipeline.py)
    │
    │  JSON-RPC over stdio
    ▼
MCP Server (mcp/ln_mcp_server.py)
    │               │
    │ bitcoin-cli   │ lightning-cli
    ▼               ▼
Bitcoin Core    Core Lightning
```

The MCP server is the **only** component with shell access. This is a deliberate security boundary: the LLM-driven pipeline can only do what the 22 named tools permit.

### Tool categories

| Category | Tools |
|----------|-------|
| System / health | `network_health`, `sys_netinfo` |
| Bitcoin Core | `btc_getblockchaininfo`, `btc_wallet_ensure`, `btc_getnewaddress`, `btc_sendtoaddress`, `btc_generatetoaddress` |
| Node lifecycle | `ln_listnodes`, `ln_node_status`, `ln_node_create`, `ln_node_start`, `ln_node_stop`, `ln_node_delete` |
| Lightning read | `ln_getinfo`, `ln_listpeers`, `ln_listfunds`, `ln_listchannels`, `ln_newaddr` |
| Lightning actions | `ln_connect`, `ln_openchannel`, `ln_invoice`, `ln_pay` |

See [Tools reference](../3.Components/TOOLS.md) for full argument and return value documentation.

### Tool schema

The tool schema is defined in `ai/tools.py` and served to the LLM in the planner's system prompt. Two formats are maintained:

- `llm_tools_schema()` — OpenAI-style function definitions for API tool use
- `llm_tools_schema_text()` — plain-text table for models that don't support structured tool use

### READ_ONLY_TOOLS

The pipeline classifies tools as read-only or state-changing. Read-only tools (`network_health`, `sys_netinfo`, `ln_getinfo`, `ln_listpeers`, `ln_listfunds`, `ln_listchannels`, `ln_listnodes`, `ln_node_status`, `btc_getblockchaininfo`, `ln_newaddr`, `btc_getnewaddress`) can be called freely. State-changing tools (`ln_node_start`, `ln_openchannel`, `ln_pay`, etc.) trigger goal verification after they complete.

---

## A2A — Agent-to-Agent Coordination

A2A describes a future architecture where multiple AI agents — each running on a separate machine with its own Lightning node — can discover each other and pay for services using Lightning Network payments.

### Design principles

**Payments as authorization.** A receiving agent requires a Lightning payment before responding to a request. The payment amount and invoice terms define the service contract. No shared secret or API key is needed.

**Payments as proof of work.** The preimage returned by a successful payment proves the receiving agent completed the task (it was the original invoice issuer). This creates an auditable record on-chain.

**Standard transport.** Agents communicate using existing Lightning primitives — keysend for push payments with a message payload, or BOLT12 offers for structured request/response flows.

### Planned scope

- Agent discovery via Lightning gossip (node alias, custom TLV records)
- Service advertisement in node metadata
- Request/response framing using keysend or BOLT12 offers
- Integration with the existing pipeline: a task routed to a remote agent returns a result that is incorporated into the local pipeline's final answer

### Current status

A2A is not yet implemented. The design is documented in [`ln-ai-network/A2A/index.md`](../../ln-ai-network/A2A/index.md).

The existing `sys_netinfo` + `ln_node_start(bind_host, announce_host)` capability is a prerequisite — it allows a node to become reachable from another machine, which is the first step toward cross-machine agent coordination.

---

## x402 — HTTP 402 Payment Required

x402 is a complementary pattern for payment-gated HTTP APIs:

1. Client makes an HTTP request
2. Server responds with `402 Payment Required` + a BOLT11 invoice
3. Client pays the invoice via its Lightning node (`ln_pay`)
4. Client retries the request with proof of payment in the header
5. Server verifies payment and returns the resource

The `ln_pay` MCP tool makes this automatable — the AI agent can handle the full 402 flow without human intervention. See [`ln-ai-network/402x/index.md`](../../ln-ai-network/402x/index.md).
