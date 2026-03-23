---
title: Roadmap
---

# Roadmap

## Implemented (current)

The core AI agent loop is complete and running:

- **4-stage pipeline**: Translator → Planner → Executor → Summarizer
- **22 MCP tools**: full Bitcoin Core + Core Lightning control surface
- **Multi-turn history**: follow-up prompts resolve without restating context
- **Goal verification**: automatic read-only confirmation after state-changing actions
- **Web UI**: real-time dashboard with Pipeline tab, Network graph, Logs, Settings
- **SSE streaming**: live pipeline events and LLM token stream
- **Cross-machine connectivity**: `sys_netinfo` + `ln_node_start` with `bind_host`/`announce_host`
- **3-backend LLM support**: OpenAI, Ollama (local), Gemini
- **Crash Kit**: one-click debug snapshot from the web UI

## Next — integration layers

- **x402 payment-gated endpoints**: HTTP middleware that issues BOLT11 invoices on 402 responses; client interceptor that pays and retries automatically. Integrated with `ln_pay` for agent-driven payment handling. See `ln-ai-network/402x/`.

- **A2A agent coordination**: multi-agent workflows where agents on separate machines discover each other and pay for services over Lightning. Request/response framing via keysend or BOLT12 offers. See `ln-ai-network/A2A/`.

- **Formalize MCP tool registry**: versioned tool schema, capability advertisement, and dynamic tool discovery for A2A scenarios.

## Later — evaluation and research

- **Metrics**: latency per pipeline stage, payment success rate, tool failure rate, LLM token counts
- **Security review**: authentication, replay protection, abuse prevention for payment-gated APIs
- **Multi-machine harness**: scripted setup for running nodes on separate machines with cross-machine peer connectivity
- **Research paper**: findings documented against SoK (Systematization of Knowledge) categories for AI agents in payment systems
