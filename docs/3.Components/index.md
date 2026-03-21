---
title: Components
---

# Components

The system is built from six distinct layers, each with a clear responsibility:

| # | Layer | Component | Status |
|---|-------|-----------|--------|
| 1 | Bitcoin base | Bitcoin Core (`bitcoind`) | Running |
| 2 | Payment layer | Core Lightning (`lightningd` × N) | Running |
| 3 | Tool boundary | MCP server (`mcp/ln_mcp_server.py`) | Running |
| 4 | Decision layer | AI pipeline (`ai/pipeline.py`) | Running |
| 5 | Payment-gated APIs | x402 patterns | Planned |
| 6 | Agent-to-agent | A2A coordination | Planned |

## Bitcoin Core

Provides the regtest blockchain. All Lightning nodes connect to the same `bitcoind` instance for block data and on-chain wallet operations. The MCP tools `btc_*` wrap `bitcoin-cli` calls.

## Core Lightning

Each Lightning node is a separate `lightningd` process with its own data directory under `runtime/lightning/node-N/`. The MCP tools `ln_*` wrap `lightning-cli` calls targeted at a specific node.

## MCP Tool Server

The execution boundary between the AI agent and the infrastructure. Exposes 22 tools over JSON-RPC (stdio). See the [Tools reference](TOOLS.md) for the complete tool list.

## AI Pipeline

A 4-stage processing loop:

```
[Translator] → IntentBlock → [Planner] → Plan → [Executor] → Results → [Summarizer] → Answer
```

All agent behavior is constrained to MCP tool calls — the pipeline has no direct shell or filesystem access to the Bitcoin/Lightning processes.

## x402 — HTTP 402 Payment Required (planned)

Payment-gated HTTP endpoints. A server returns 402 with a Lightning invoice; the client pays automatically and retries with proof of payment. See [`ln-ai-network/402x/index.md`](../../ln-ai-network/402x/index.md).

## A2A — Agent-to-Agent coordination (planned)

Multiple AI agents communicating over the Lightning Network, using payments as authorization and proof of work. See [`ln-ai-network/A2A/index.md`](../../ln-ai-network/A2A/index.md).

---

- [MCP Tools reference](TOOLS.md)
