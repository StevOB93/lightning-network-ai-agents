# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A research harness for AI agents that use tools (MCP), transact value (Lightning/x402), and coordinate (A2A) on a local Bitcoin regtest stack. Academic project (CSCI 499).

## Commands

All scripts run from `ln-ai-network/` and target WSL/Linux (bash). The system uses a numbered boot sequence.

### Environment Setup
```bash
cd ln-ai-network
cp .env.example .env        # then edit .env — set your chosen provider's API key
source env.sh               # loads .env + sets runtime paths; warns on missing keys
```

### Install (one-time)
```bash
./scripts/0.install.sh      # installs Bitcoin Core, builds Core Lightning from source, creates venv, installs Python deps
```

### Start / Stop
```bash
./scripts/1.start.sh [NODE_COUNT]       # boot full system (infra → control plane → agents)
./scripts/shutdown.sh [NODE_COUNT]       # shutdown in reverse order
./scripts/restart_agent.sh               # restart agent keeping inbox/outbox
./scripts/restart_agent.sh fresh         # archive + clear inbox/outbox, then restart
```

### Network Test
```bash
./scripts/network_test.sh [NODE_COUNT]   # deterministic bring-up: fund nodes, connect linear topology, open channels, wait for CHANNELD_NORMAL
```

### Mine Blocks
```bash
./scripts/tools/mine_blocks.sh [COUNT]
```

### Run Tests (CI runs these automatically on push to main)
```bash
cd ln-ai-network
python -m pytest ai/tests/ -v                                    # full suite (no API keys needed)
python -m pytest ai/tests/test_ollama_backend.py -v             # single file
python -m pytest ai/tests/test_factory.py::TestFactoryRouting   # single class
```

### Front-End Demo UI
```bash
cd ln-ai-network
python scripts/demo_ui_server.py   # serves web/ at http://127.0.0.1:8008
# Override: DEMO_UI_HOST=0.0.0.0 DEMO_UI_PORT=9000 python scripts/demo_ui_server.py
```

### Run Agent Offline (Mock Mode)
```bash
cd ln-ai-network
python -m ai.agent   # runs against mock fixtures in ai/mocks/fixtures/
```

## Architecture

### Data Flow
```
User → inbox.jsonl → Agent (agent.py) → LLM Backend → tool_calls → MCP Server → bitcoin-cli / lightning-cli
                                                                                       ↓
User ← outbox.jsonl ← Agent ← structured intent JSON ←────────────────────── tool results
```

### Core Components

- **`ln-ai-network/ai/agent.py`** — Main agent controller. Reads from `runtime/agent/inbox.jsonl`, writes reports to `runtime/agent/outbox.jsonl`. Enforces deterministic safety policies (fail-fast on tool errors, oscillation detection, redundant recall blocking, tool-arg normalization). Build tag: `clean-single-agent-v5`.

- **`ln-ai-network/mcp/ln_mcp_server.py`** — MCP server exposing 23 tools (bitcoin: `btc_*`, lightning: `ln_*`, health: `network_health`). All tools call `bitcoin-cli` or `lightning-cli` via subprocess. Listens on stdin (JSON-RPC).

- **`ln-ai-network/ai/llm/`** — LLM backend abstraction. `factory.py` selects backend via `LLM_PROVIDER` env var (reads `LLM_PROVIDER` first, then legacy `LLM_BACKEND`). Adapters:
  - `adapters/ollama_backend.py` — local Ollama (default; conforms to `LLMBackend` ABC)
  - `adapters/openai_backend.py` — OpenAI API (known contract mismatch: `step()` takes raw dicts, not `LLMRequest`)
  - `adapters/gemini_backend.py` — Google Gemini native SDK (uses `run_prompt()` path, not `step()`)
  - Interface defined in `base.py` (`LLMBackend` ABC, `LLMRequest`, `LLMResponse`, error taxonomy)

- **`ln-ai-network/ai/core/`** — Shared utilities: `backoff.py` (exponential + circuit breaker), `rate_limiter.py` (RPM/TPM), `scheduler.py`, `token_estimation.py`, `config.py`, `concurrency.py`. Currently exist but are **not yet wired into `agent.py`**.

- **`ln-ai-network/ai/command_queue.py`** — File-based JSONL queue with byte-offset tracking for deterministic reads.

- **`ln-ai-network/ai/mcp_client.py`** — `MCPClient` protocol + `FixtureMCPClient` (deterministic mock for tests) + `FastMCPClientWrapper`.

- **`ln-ai-network/scripts/demo_ui_server.py`** — stdlib Python HTTP server serving `web/` static files and bridging `/api/status`, `/api/health`, `/api/ask` to the agent's JSONL inbox/outbox.

- **`ln-ai-network/web/`** — Front-end demo dashboard (vanilla HTML/CSS/JS). Polls `/api/status` for live agent state.

### Boot Sequence (scripts/startup/)
1. `0.1.infra_boot.sh` — bitcoind + regtest chain
2. `0.2.control_plane_boot.sh` — MCP server + agent control plane
3. `0.3.agent_boot.sh` — AI agent process

Shutdown reverses this order via `scripts/shutdown/`.

## Key Conventions

- **MCP boundary**: The AI agent never executes shell commands directly. All actions go through MCP tools. The agent reads via MCP and outputs structured intent JSON only.
- **regtest only**: All Bitcoin/Lightning operations run on regtest. Never mainnet.
- **Secrets in `.env`**: Real API keys go in `ln-ai-network/.env` (gitignored). Use `.env.example` as template. `env.sh` warns if key is missing or still placeholder.
- **LLM provider toggle**: Set `LLM_PROVIDER=ollama` (default), `openai`, or `gemini` in `.env`. Gate all LLM usage with `ALLOW_LLM=1`. Legacy env var `LLM_BACKEND` is also accepted.
- **Deterministic design**: Trace logging, byte-offset queue cursors, deterministic jitter (not random), signature-based dedup for tool calls.
- **Fail-fast on tool errors**: If an MCP tool returns an error, the agent stops and emits `noop` rather than retrying blindly.
- **Runtime state is ephemeral**: The `runtime/` directory (blockchain data, node configs, agent logs) is gitignored and recreated by scripts.
- **Mock fixtures**: Test without live infrastructure using JSON fixtures in `ai/mocks/fixtures/` (scenarios: `healthy.json`, `no_route.json`, `liquidity_starved.json`, `tool_failure.json`).
- **Known issue — LLM interface contract**: `OpenAIBackend.step()` and `GeminiBackend.step()` do not conform to the `LLMBackend` ABC signature (`LLMRequest` → `LLMResponse`). Fixing this is a prerequisite for clean multi-model support (task J1–J4 in the team task list).
- **CI**: GitHub Actions runs `pytest ai/tests/` on every push to `main` and on PRs. All tests in `ai/tests/` must pass without API keys or live infrastructure.
