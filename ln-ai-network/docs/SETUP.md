# Setup & Installation Guide

This guide walks through setting up the Lightning Network AI Agent system from scratch. The project runs on Linux or WSL (Windows Subsystem for Linux).

## Prerequisites

- **OS**: Ubuntu 22.04+ (native Linux or WSL2)
- **Python**: 3.10+ (3.11+ recommended)
- **Git**: any recent version
- **Disk**: ~5 GB (Bitcoin Core binaries + Core Lightning build + Python deps)
- **RAM**: 4 GB minimum, 8 GB recommended

## Quick Start

```bash
# Clone the repository
git clone https://github.com/StevOB93/lightning-network-ai-agents.git
cd lightning-network-ai-agents/ln-ai-network

# Install Bitcoin Core, Core Lightning, and Python deps (one-time, ~20 min)
./scripts/0.install.sh

# Configure your LLM provider
cp .env.example .env
# Edit .env — set your API key (see "LLM Provider Setup" below)
source env.sh

# Start everything (Bitcoin nodes, Lightning nodes, MCP server, AI agent, web UI)
./scripts/1.start.sh

# Open the dashboard
# http://127.0.0.1:8008
```

## Step-by-Step Installation

### 1. Install dependencies

The install script handles everything automatically:

```bash
./scripts/0.install.sh
```

This installs:
- **Bitcoin Core** (official binaries) — the regtest blockchain backend
- **Core Lightning** (built from source) — Lightning Network implementation
- **Python virtual environment** at `.venv/` with all pip packages from `requirements.txt`

If the script fails partway through, it's safe to re-run — it skips already-installed components.

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and set your chosen LLM provider's API key. The minimum configuration is:

```bash
LLM_BACKEND=openai        # or: ollama, gemini, claude
OPENAI_API_KEY=sk-...      # your real key
```

Then load the environment:

```bash
source env.sh
```

`env.sh` reads `.env`, exports all variables, and warns if required keys are missing or still set to placeholder values.

### 3. Start the system

```bash
./scripts/1.start.sh [NODE_COUNT]
```

`NODE_COUNT` defaults to 2. The script boots in order:
1. `bitcoind` (regtest chain)
2. Core Lightning nodes (one per node)
3. MCP server (exposes 28 tools via JSON-RPC)
4. AI pipeline agent
5. Web UI server on port 8008

### 4. Set up the network

```bash
./scripts/network_test.sh [NODE_COUNT]
```

This funds all nodes from the regtest faucet, connects them in a linear topology, opens channels between each pair, mines blocks until all channels reach `CHANNELD_NORMAL`, and verifies the network is fully operational.

### 5. Open the dashboard

Navigate to **http://127.0.0.1:8008** in your browser. The dashboard shows:
- **Agent tab**: submit prompts and see pipeline results
- **Pipeline tab**: per-stage translator/planner/executor status
- **Network tab**: D3 force graph of Lightning node topology
- **Logs tab**: live trace events and archived query history
- **Settings tab**: LLM backend/model configuration

### 6. Shut down

```bash
./scripts/shutdown.sh [NODE_COUNT]
```

Shuts down in reverse order (UI server, agent, MCP server, Lightning nodes, bitcoind).

## LLM Provider Setup

The system supports four LLM backends. Set `LLM_BACKEND` in `.env` to your choice.

### OpenAI (recommended for best results)

```bash
LLM_BACKEND=openai
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o          # or gpt-4o-mini for lower cost
```

Get your key at [platform.openai.com/api-keys](https://platform.openai.com/api-keys).

### Anthropic (Claude)

```bash
LLM_BACKEND=claude
ANTHROPIC_API_KEY=sk-ant-...
CLAUDE_MODEL=claude-opus-4-6   # or claude-sonnet-4-6
```

Get your key at [console.anthropic.com](https://console.anthropic.com).

### Google Gemini

```bash
LLM_BACKEND=gemini
GEMINI_API_KEY=AI...
GEMINI_MODEL=gemini-2.5-flash
```

Get your key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey).

### Ollama (local, free, no API key)

```bash
LLM_BACKEND=ollama
OLLAMA_MODEL=llama3.2:3b
OLLAMA_BASE_URL=http://127.0.0.1:11434
```

Install Ollama and pull a model:
```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull llama3.2:3b
```

Ollama runs entirely locally with no API key. Quality is lower than cloud models but useful for development and offline work.

### Per-Stage Overrides

You can use different models for different pipeline stages:

```bash
# Use a fast model for translation, a powerful one for planning
TRANSLATOR_LLM_BACKEND=gemini
TRANSLATOR_GEMINI_MODEL=gemini-2.5-flash
PLANNER_LLM_BACKEND=openai
PLANNER_OPENAI_MODEL=gpt-4o
```

Available stage prefixes: `TRANSLATOR_`, `PLANNER_`, `EXECUTOR_`, `SUMMARIZER_`.

## Security Setup (Optional)

For authenticated access to the web dashboard:

```bash
bash scripts/setup_security.sh
```

This prompts for an admin password and configures session-based authentication, CSRF protection, and optionally generates a self-signed TLS certificate. See `docs/SECURITY.md` for details.

## WSL-Specific Notes

### Port Forwarding

WSL2 runs in a separate network namespace. To access the dashboard from your Windows browser:

```bash
# The UI server binds to 127.0.0.1:8008 by default.
# In most WSL2 setups, localhost forwarding works automatically.
# If not, set UI_HOST=0.0.0.0 in .env to bind to all interfaces.
```

### Filesystem Performance

Avoid placing the project directory on a Windows filesystem mount (`/mnt/c/...`). The 9P filesystem bridge is ~10x slower than native ext4. Keep the project under your WSL home directory (`~/`).

### Clock Drift

WSL2 can experience clock drift when the host machine sleeps. If Lightning nodes fail to start with timestamp errors:

```bash
sudo hwclock -s
```

## Running Tests

Tests run without any API keys or live infrastructure:

```bash
source .venv/bin/activate
python -m pytest ai/tests/ -v
```

The CI pipeline runs the same command on every push to `main`.

## Project Structure

```
ln-ai-network/
  ai/                    # Python AI agent code
    controllers/         # Pipeline stages (translator, planner, executor, summarizer)
    core/                # Infrastructure (config, rate limiter, backoff, registry)
    llm/                 # LLM backend adapters (openai, gemini, ollama, claude)
    tests/               # Test suite (419 tests)
    pipeline.py          # PipelineCoordinator — main orchestrator
    command_queue.py     # File-based JSONL message bus
  mcp/                   # MCP server (28 tools: btc_*, ln_*, network_health)
  scripts/               # Boot/shutdown scripts, UI server, security module
    startup/             # Numbered boot sequence (0.1, 0.2, 0.3, 0.4)
    shutdown/            # Reverse shutdown sequence
    ui_server.py         # Web dashboard HTTP server
    security.py          # Auth, CSRF, rate limiting, RBAC, audit logging
  web/                   # Frontend (HTML/CSS/JS, D3 network graph)
  runtime/               # Ephemeral state (gitignored): blockchain, node configs, logs
  .env.example           # Environment template (safe to commit)
  .env                   # Your real config (gitignored, never commit)
```

## Troubleshooting

### "lightningd won't start" / "Connection refused"

Bitcoin Core must be fully initialized before Lightning nodes can connect. The start script handles this, but if you're starting components manually:

```bash
# Start bitcoind first and wait for it to be ready
bitcoind -regtest -daemon
bitcoin-cli -regtest getblockchaininfo  # Should succeed before starting lightningd
```

### "API key not working" / "AuthError"

1. Check that your key is in `.env` (not `.env.example`)
2. Run `source env.sh` — it will warn if the key is missing or still a placeholder
3. Verify the key works: `curl -s https://api.openai.com/v1/models -H "Authorization: Bearer $OPENAI_API_KEY" | head -1`

### "MCP timeout" / "Tool call failed"

The MCP server calls `bitcoin-cli` and `lightning-cli` via subprocess. If these time out:

1. Check that nodes are running: `bitcoin-cli -regtest getblockchaininfo`
2. Increase timeout: set `MCP_CALL_TIMEOUT_S=60` in `.env`
3. Check for stuck processes: `ps aux | grep -E 'bitcoind|lightningd'`

### "Pipeline lock already held"

Another pipeline instance is running. Either shut it down first or check for stale lock files:

```bash
cat runtime/agent/pipeline.lock
# If the PID listed is not running, the lock is stale:
rm runtime/agent/pipeline.lock
```

### Tests fail with import errors

Make sure you're using the project virtual environment:

```bash
source .venv/bin/activate
python -m pytest ai/tests/ -v
```
