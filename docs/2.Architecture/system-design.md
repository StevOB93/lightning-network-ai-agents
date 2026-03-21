---
title: System Design
---

# System Design

## Data flow — prompt to answer

```
User types prompt
        │
        ▼
POST /api/ask → writes JSON line to runtime/agent/inbox.jsonl
        │
        │  (pipeline polls inbox every 0.5s)
        ▼
ai/pipeline.py reads new inbox entry
        │
        ├─► [1. Translator]
        │       LLM call: prompt + conversation history → IntentBlock
        │       IntentBlock: { goal, intent_type, context, success_criteria }
        │
        ├─► [2. Planner]
        │       LLM call: IntentBlock + tool schema → ExecutionPlan
        │       ExecutionPlan: [ { tool, args, rationale }, ... ]
        │
        ├─► [3. Executor]
        │       For each step in plan:
        │         - Validate and normalize args
        │         - JSON-RPC call to MCP server (stdin/stdout)
        │         - MCP server runs bitcoin-cli or lightning-cli
        │         - Collect result; chain output to next step if needed
        │       After state-changing steps: run verification read call
        │
        └─► [4. Summarizer]
                LLM call: tool results + goal → human-readable summary
                Outputs: { summary_text, success, timestamp }
                        │
                        ▼
        Writes PipelineResult to runtime/agent/outbox.jsonl
        SSE stream pushes result to web UI
```

## Pipeline files

| File | Purpose |
|------|---------|
| `ai/pipeline.py` | Main loop: polls inbox, runs stages, writes outbox |
| `ai/agent.py` | LLM client wrapper; handles streaming, tool use, retries |
| `ai/models.py` | Pydantic models: IntentBlock, ExecutionPlan, StepResult, PipelineResult |
| `ai/tools.py` | Tool schemas (for LLM), TOOL_REQUIRED map, READ_ONLY_TOOLS set |
| `ai/controllers/translator.py` | Stage 1: text → IntentBlock |
| `ai/controllers/planner.py` | Stage 2: IntentBlock → ExecutionPlan |
| `ai/controllers/executor.py` | Stage 3: plan → tool call results |
| `ai/controllers/summarizer.py` | Stage 4: results → human answer |
| `ai/controllers/shared.py` | JSON repair, LLM parsing helpers shared across stages |

## MCP tool server

`mcp/ln_mcp_server.py` is the execution boundary. It:

- Accepts JSON-RPC requests over stdin
- Runs `bitcoin-cli` or `lightning-cli` subprocess calls
- Returns `{"ok": bool, "payload": ...}` for every tool

The pipeline calls it via `ai/mcp_client.py`, which spawns the server as a subprocess and communicates over stdio. Each tool call is synchronous with a configurable timeout (`MCP_CALL_TIMEOUT_S`, default 30s).

## Runtime layout

```
ln-ai-network/runtime/
  node_count              # NODE_COUNT used at start (e.g. "2")
  mcp.pid                 # MCP server PID
  ui_server.pid           # Web UI server PID
  agent/
    agent.pid             # Pipeline process PID
    pipeline.lock         # Single-instance lock (contains pid=NNNN)
    inbox.jsonl           # Incoming prompt queue
    inbox.offset          # Read cursor (byte offset into inbox.jsonl)
    outbox.jsonl          # Pipeline results (all history)
    history.jsonl         # Conversation history (last N turns)
    trace.log             # Live trace events for current/last run (JSON lines)
    stream.jsonl          # LLM token stream for the UI /api/tokens endpoint
  bitcoin/
    shared/               # bitcoind data directory
      bitcoin.conf
      regtest/
  lightning/
    node-1/               # lightningd data
      config
      hsm_secret
      regtest/
    node-2/
      ...
```

## LLM call graph

The pipeline makes 3 LLM calls per prompt (when all stages succeed):

```
Translator  →  1 call  →  IntentBlock (structured JSON)
Planner     →  1 call  →  ExecutionPlan (structured JSON)
Summarizer  →  1 call  →  plain-text answer
```

The Executor makes **zero** LLM calls — it is a deterministic dispatch loop.

All LLM calls share the same backend (`LLM_BACKEND`), model, and temperature settings. The system prompt for each stage is defined in the corresponding controller file.

## Conversation history

After each pipeline run, the prompt and result are appended to `runtime/agent/history.jsonl`. On subsequent prompts, the last N turns are included in the Translator and Planner system prompts. This enables follow-up prompts to reference previous context without restating it.

## SSE streaming

The web UI connects to `/api/stream` (a Server-Sent Events endpoint). The server pushes three event types:

| Event | When | Payload |
|-------|------|---------|
| `status` | Every ~2s + on outbox change | Agent lock, last request ID, inbox/outbox counts |
| `pipeline_result` | On new outbox entry | Full PipelineResult |
| `trace` | On new trace events | List of recent trace events |

The LLM token stream is available separately on `/api/tokens` (tails `stream.jsonl`).

## Security boundary

The MCP server is the only component with shell access. The AI agent cannot run arbitrary commands — it can only call the 22 named tools in `ln_mcp_server.py`. Each tool:

- Has a fixed signature validated by the server
- Calls exactly one `bitcoin-cli` or `lightning-cli` command
- Returns structured JSON; no raw shell output is passed back

This isolates LLM-driven behavior from the host filesystem and process environment.
