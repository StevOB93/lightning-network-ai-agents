# ln-ai-network

A local Lightning Network test environment with an AI agent that understands and executes natural language commands against it. Run two (or more) regtest Lightning nodes, then ask the agent to open channels, rebalance liquidity, pay invoices, or run diagnostics — in plain English.

---

## What it does

**Infrastructure layer** — manages a Bitcoin Core regtest node and one or more Core Lightning nodes on your local machine. Everything runs locally; no real funds, no mainnet exposure.

**MCP control plane** — a Model Context Protocol server (`mcp/ln_mcp_server.py`) wraps every Bitcoin and Lightning operation as a typed, validated tool call. The AI agent is only ever allowed to act through these tools — no direct shell access.

**AI pipeline** — a 4-stage natural language pipeline:
1. **Translator** — parses a plain English prompt into a structured `IntentBlock` (goal, intent type, required context, success criteria)
2. **Planner** — converts the `IntentBlock` into an ordered `ExecutionPlan` of MCP tool calls, with placeholder chaining between steps
3. **Executor** — runs each step against the MCP server, applying per-step retry/skip/abort policies and resolving `$step.result` placeholders at runtime
4. **Summarizer** — synthesises tool results into a human-readable answer

**Web UI** — a real-time dashboard at `http://127.0.0.1:8008` showing pipeline stage outputs, a live D3 network graph, a full trace log, inbox/outbox queues, and a settings panel.

---

## Architecture

```
  User (browser / CLI)
       │  prompt
       ▼
  inbox.jsonl ──── PipelineCoordinator ────────────────────────────────┐
                        │                                               │
               ┌────────▼────────┐                                     │
               │   Translator    │  LLM → IntentBlock                  │
               └────────┬────────┘                                     │
               ┌────────▼────────┐                                     │
               │    Planner      │  LLM → ExecutionPlan                │
               └────────┬────────┘                                     │
               ┌────────▼────────┐                                     │
               │    Executor     │  MCP calls → StepResults            │
               └────────┬────────┘                                     │
               ┌────────▼────────┐                                     │
               │   Summarizer    │  LLM → human answer                 │
               └────────┬────────┘                                     │
                        │                                               │
                  outbox.jsonl ◄─────────────────────────────────────┘
                        │
                  Web UI (SSE)
```

Each stage uses an independently configured LLM backend. The MCP server runs as a separate process; the agent communicates with it over stdio.

---

## Directory layout

```
ln-ai-network/
├── scripts/
│   ├── 0.install.sh          # One-time dependency installer
│   ├── 1.start.sh            # Full system start (infra + agent + UI)
│   ├── shutdown.sh           # Full system stop
│   ├── restart_agent.sh      # Restart AI agent only (keeps infra running)
│   ├── startup/              # Per-component boot scripts (called by 1.start.sh)
│   │   ├── 0.1.infra_boot.sh
│   │   ├── 0.2.control_plane_boot.sh
│   │   ├── 0.3.agent_boot.sh
│   │   └── 0.4.ui_server.sh
│   └── shutdown/             # Per-component stop scripts (called by shutdown.sh)
├── mcp/
│   └── ln_mcp_server.py      # MCP server: all Bitcoin + Lightning tools
├── ai/
│   ├── pipeline.py           # 4-stage pipeline coordinator (main entry point)
│   ├── controllers/          # Translator, Planner, Executor, Summarizer
│   ├── llm/                  # LLM backend adapters (ollama, openai, gemini)
│   ├── models.py             # Shared dataclasses
│   └── tools.py              # Tool registry, normalization, schema generation
├── web/
│   ├── index.html            # Single-page application shell
│   ├── app.js                # All UI logic (SSE, D3, pipeline rendering)
│   └── styles.css            # Dark-theme design system
├── scripts/ui_server.py      # Lightweight HTTP + SSE server for the web UI
├── env.sh                    # Deterministic environment variables (sourced by all scripts)
├── .env                      # Local overrides — not committed (copy from .env.example)
└── runtime/                  # Created at runtime — not committed
    ├── bitcoin/shared/       # bitcoind data directory
    ├── lightning/node-N/     # lightningd data per node
    ├── agent/                # Agent state: inbox, outbox, trace, lock files
    └── node_count            # Active node count written by start.sh
```

---

## Quick start

```bash
# 1. Install dependencies (once)
./scripts/0.install.sh

# 2. Configure your LLM (copy and edit)
cp .env.example .env
# set LLM_BACKEND=openai and OPENAI_API_KEY=sk-...
# or set LLM_BACKEND=ollama and OLLAMA_MODEL=llama3

# 3. Start the full system with 2 Lightning nodes
./scripts/1.start.sh 2

# 4. Open the web UI
# → http://127.0.0.1:8008

# 5. Stop everything
./scripts/shutdown.sh
```

---

## Sending prompts

From the **Web UI**: type a prompt in the Agent tab and press Enter.

From the **CLI**:
```bash
python3 -c "from ai.command_queue import enqueue; enqueue('open a channel from node 1 to node 2 with 500000 sat')"
# Check result
tail -n 1 runtime/agent/outbox.jsonl | python3 -m json.tool
```

---

## Key environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_BACKEND` | `openai` | LLM provider: `openai`, `ollama`, `gemini` |
| `OPENAI_API_KEY` | — | Required when `LLM_BACKEND=openai` |
| `GEMINI_API_KEY` | — | Required when `LLM_BACKEND=gemini` |
| `OLLAMA_MODEL` | — | Model name when using Ollama (e.g. `llama3`) |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Ollama API endpoint |
| `BITCOIN_RPC_PORT` | `18443` | Bitcoin Core regtest RPC port |
| `LIGHTNING_BASE_PORT` | `9735` | Base port for Lightning nodes (node N uses port 9735+N) |
| `LN_BIND_HOST` | `127.0.0.1` | Interface lightningd binds its peer port on |
| `LN_ANNOUNCE_HOST` | `LN_BIND_HOST` | Address advertised to peers (override for cross-machine) |
| `UI_PORT` | `8008` | Web UI port |
| `ALLOW_LLM` | `1` | Set to `0` to block all LLM calls (dry-run mode) |

All variables can be set in `.env` — see `.env.example` for the full list.

---

## Status

Active development. The pipeline, MCP server, and web UI are all functional.
See `ai/README.md`, `mcp/README.md`, and `web/README.md` for subsystem details.
