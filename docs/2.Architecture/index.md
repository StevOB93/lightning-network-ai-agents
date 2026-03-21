---
title: Architecture
---

# Architecture

## System overview

```
┌──────────────────────────────────────────────────────────────────┐
│                         Web UI (port 8008)                       │
│        Prompt → Pipeline tab → Network graph → Logs tab          │
└──────────────────────┬───────────────────────────────────────────┘
                       │ POST /api/ask
                       ▼
┌──────────────────────────────────────────────────────────────────┐
│                    AI Pipeline (ai/pipeline.py)                  │
│                                                                  │
│  [Translator] → IntentBlock → [Planner] → Plan → [Executor]      │
│       │                                              │           │
│    LLM call                                      MCP calls       │
│       │                          ┌───────────────────┘           │
│       └──────────────────────────▼                               │
│                           [Summarizer] → human-readable answer   │
│                              LLM call                            │
└──────────────────────────────────┬───────────────────────────────┘
                                   │ JSON-RPC over stdio
                                   ▼
┌──────────────────────────────────────────────────────────────────┐
│                   MCP Tool Server (mcp/ln_mcp_server.py)         │
│                      22 tools in 5 categories                    │
└────────────┬───────────────────────────────────────┬─────────────┘
             │ bitcoin-cli (RPC)                     │ lightning-cli (RPC)
             ▼                                       ▼
┌────────────────────┐              ┌────────────────────────────────┐
│    Bitcoin Core    │              │   Core Lightning nodes (1..N)  │
│   (regtest)        │◄─────────────│   lightningd × NODE_COUNT      │
│   bitcoind         │  block events│                                │
└────────────────────┘              └────────────────────────────────┘
```

## Layers

| Layer | Component | Description |
|-------|-----------|-------------|
| UI | `scripts/ui_server.py` + `web/` | HTTP + SSE dashboard; prompt input, pipeline view, network graph, logs |
| AI pipeline | `ai/pipeline.py` + `ai/controllers/` | 4-stage processing loop: Translator → Planner → Executor → Summarizer |
| Tool boundary | `mcp/ln_mcp_server.py` | JSON-RPC over stdio; the only component that runs shell commands |
| Base layer | `bitcoind` + `lightningd` × N | Regtest Bitcoin and Lightning Network |

## Key design decisions

**The agent ONLY acts via MCP tools.** No shell access, no direct file writes. Every Bitcoin/Lightning operation goes through `ln_mcp_server.py`, which validates arguments and invokes `bitcoin-cli` or `lightning-cli`.

**File-based queue.** Prompts arrive as JSON lines in `runtime/agent/inbox.jsonl`. Results are written to `runtime/agent/outbox.jsonl`. The web UI writes to the inbox; the pipeline reads it. This decouples the UI from the pipeline and makes the system inspectable.

**Multi-turn history.** The last N exchanges are included as context on every LLM call. Follow-up prompts like "now pay that invoice" resolve naturally.

**Goal verification.** After state-changing operations (payment, channel open), the Executor runs a read-only verification step to confirm the action succeeded before returning.

## Pages

- [System design detail](system-design.md) — data flow, file layout, LLM call graph
