---
title: Local Lightning Network Harness (ln-ai-network)
---

# Local Lightning Network Harness (ln-ai-network)

The `ln-ai-network/` directory contains the full runnable harness: Bitcoin + Lightning infrastructure, the AI agent pipeline, the MCP tool server, and the web UI.

## Installation

From the repo root, run the one-time installer:

```bash
./install.sh
```

This installs Bitcoin Core, Core Lightning, and sets up the Python virtual environment at `ln-ai-network/.venv/`. The install log is saved to `ln-ai-network/logs/install.log`.

To force-reinstall Python dependencies on the next start:

```bash
REINSTALL_PY_DEPS=1 ./start.sh
```

## Configuration

```bash
cp ln-ai-network/.env.example ln-ai-network/.env
```

Edit `ln-ai-network/.env`. Key settings:

| Variable | Default | Description |
|----------|---------|-------------|
| `ALLOW_LLM` | `0` | Set to `1` to enable LLM calls |
| `LLM_BACKEND` | `openai` | `openai`, `ollama`, or `gemini` |
| `OPENAI_API_KEY` | — | Required for OpenAI |
| `OPENAI_MODEL` | `gpt-4o` | OpenAI model name |
| `GEMINI_API_KEY` | — | Required for Gemini |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Ollama endpoint |
| `OLLAMA_MODEL` | `llama3.2` | Ollama model name |
| `LLM_TEMPERATURE` | `0` | 0 = deterministic, recommended |
| `MCP_CALL_TIMEOUT_S` | `30` | Timeout for each MCP tool call |
| `LIGHTNING_BASE_PORT` | `9735` | Base port; node N uses `9735 + N` |
| `UI_HOST` | `127.0.0.1` | Web UI bind address |
| `UI_PORT` | `8008` | Web UI port |

## Starting and stopping

```bash
./start.sh          # Start with 2 Lightning nodes (default)
./start.sh 3        # Start with 3 nodes

./stop.sh           # Stop everything cleanly
```

The web UI opens automatically at `http://127.0.0.1:8008` on Linux/WSL2.

## Restarting the agent only

If you change pipeline code, restart only the AI agent without touching the Bitcoin/Lightning infrastructure:

```bash
cd ln-ai-network
./scripts/restart_agent.sh          # keep inbox/outbox
./scripts/restart_agent.sh fresh    # clear queue state
```

## Directory layout

```
ln-ai-network/
├── ai/                  # AI pipeline (translator, planner, executor, summarizer)
│   ├── pipeline.py      # Main pipeline loop
│   ├── agent.py         # Agent entry point
│   ├── tools.py         # Tool schemas and metadata
│   ├── models.py        # Pydantic models (IntentBlock, ExecutionPlan, etc.)
│   ├── controllers/     # translator.py, planner.py, executor.py, summarizer.py
│   └── tests/           # pytest test suite
├── mcp/                 # MCP tool server
│   └── ln_mcp_server.py # 22 tools across bitcoin-cli / lightning-cli
├── scripts/             # Boot scripts and utilities
│   ├── 0.install.sh     # One-time install (called by ./install.sh)
│   ├── 1.start.sh       # Full system launcher (called by ./start.sh)
│   ├── shutdown.sh      # Graceful shutdown (called by ./stop.sh)
│   ├── restart_agent.sh # Agent-only restart
│   └── ui_server.py     # HTTP + SSE web server
├── web/                 # Frontend (HTML, JS, CSS)
├── .env.example         # Template — copy to .env
└── runtime/             # Created at runtime
    ├── agent/           # inbox, outbox, trace, lock files
    ├── bitcoin/         # bitcoind data
    └── lightning/       # lightningd data (one dir per node)
```

## Source and documentation

- [Source directory](https://github.com/StevOB93/lightning-network-ai-agents/tree/main/ln-ai-network)
- [Install script reference](../../ln-ai-network/scripts/README_INSTALL.md)
- [Start script reference](../../ln-ai-network/scripts/README_START.md)
- [Web UI reference](../../ln-ai-network/web/README.md)
