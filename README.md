# Lightning Network AI Agent

A prompt-driven AI agent that autonomously executes Lightning Network workflows on regtest. Type a plain-English instruction — the agent plans and runs every step via MCP tools.

**The agent ONLY acts via MCP tools. No direct shell access.**

---

## Quick Start

### Step 1 — One-time setup (first time only)

```bash
./setup.sh
```

Installs Bitcoin Core, Core Lightning, Python venv, and all dependencies.

### Step 2 — Configure your LLM

```bash
cp ln-ai-network/.env.example ln-ai-network/.env
```

Edit `ln-ai-network/.env` and set your LLM key:

| Backend | What to set |
|---------|------------|
| OpenAI (default) | `OPENAI_API_KEY=sk-...` and `ALLOW_LLM=1` |
| Ollama (local, free) | `LLM_BACKEND=ollama` and `ALLOW_LLM=1` |
| Gemini | `GEMINI_API_KEY=...` and `LLM_BACKEND=gemini` and `ALLOW_LLM=1` |

### Step 3 — Start

```bash
./run.sh
```

The system starts, and the **web UI opens automatically** at `http://127.0.0.1:8008`.

### Step 4 — Stop

```bash
./stop.sh
```

Cleanly shuts down Bitcoin, Lightning, and the AI pipeline.

---

## Using the Web UI

The dashboard at `http://127.0.0.1:8008` is the main interface.

Type any Lightning Network instruction into the prompt box and press **Queue Request** (or Ctrl+Enter):

```
Check the network health and tell me the status of all nodes.
```

```
Open a 500,000 sat channel from node 1 to node 2.
```

```
Have node 2 create an invoice for 10,000 msat, then pay it from node 1.
```

The dashboard shows live:
- **Pipeline stage cards** — Translator → Intent, Planner → Steps, Executor → Results
- **Network graph** — nodes and channels, auto-populated from tool results
- **Live trace log** — real-time event stream
- **Agent summary** — final answer from the agent

---

## Architecture

```
Prompt → [Translator] → Intent → [Planner] → Plan → [Executor] → Results
```

| Stage | Does | Uses LLM? |
|-------|------|-----------|
| Translator | Text → structured IntentBlock | Yes |
| Planner | IntentBlock → ordered tool steps | Yes |
| Executor | Runs MCP tool calls, retries, chaining | No |

**Multi-turn**: the last 4 exchanges are carried as context — follow-up prompts like "now pay that invoice" work naturally.

**Goal verification**: after payment/channel operations, a read-only check confirms the action succeeded.

---

## Running Tests

```bash
cd ln-ai-network
source .venv/bin/activate
python -m pytest ai/tests/ -v
```

---

## Development Commands

| Command | What it does |
|---------|-------------|
| `./run.sh` | Start the full system |
| `./stop.sh` | Stop everything cleanly |
| `./setup.sh` | One-time install |
| `./run.sh 3` | Start with 3 Lightning nodes |
| `cd ln-ai-network && ./scripts/restart_agent.sh` | Restart just the AI pipeline (no infra restart) |
| `cd ln-ai-network && ./scripts/restart_agent.sh fresh` | Restart with cleared inbox/outbox |

---

## Logs

| File | Contents |
|------|---------|
| `ln-ai-network/runtime/agent/trace.log` | Per-prompt trace (resets each request) |
| `ln-ai-network/runtime/agent/outbox.jsonl` | Pipeline results (all history) |
| `ln-ai-network/logs/system/0.3.agent_boot.log` | Pipeline process log |
| `ln-ai-network/logs/system/0.4.ui_server.log` | Web UI server log |
| `ln-ai-network/logs/system/shutdown.log` | Shutdown log |

---

## Troubleshooting

**Web UI doesn't open automatically**
Navigate manually to `http://127.0.0.1:8008`. On WSL, ensure your Windows browser can reach localhost.

**"OPENAI_API_KEY not set" error**
Copy `.env.example` to `.env` and set a real API key, or switch to Ollama with `LLM_BACKEND=ollama`.

**Agent not responding to prompts**
Check `ln-ai-network/logs/system/0.3.agent_boot.log`. Ensure `ALLOW_LLM=1` is set in `.env`.

**bitcoind / lightningd not found**
Run `./setup.sh` to install the required binaries.

**Port conflicts**
Override ports in `.env`: `BITCOIN_RPC_PORT`, `LIGHTNING_BASE_PORT`, `UI_PORT`.

---

## Project Layout

```
lightning-network-ai-agents/
├── run.sh              ← START HERE
├── stop.sh             ← stop everything
├── setup.sh            ← one-time install
└── ln-ai-network/
    ├── ai/             # Pipeline: translator, planner, executor, models, LLM backends
    ├── mcp/            # MCP tool server (bitcoin-cli / lightning-cli boundary)
    ├── scripts/        # Start, stop, install, startup sequence
    │   └── ui_server.py
    ├── web/            # Frontend (HTML, JS, CSS)
    ├── .env.example    # Copy to .env and fill in secrets
    └── runtime/        # Created at runtime (inbox, outbox, logs, locks)
```
