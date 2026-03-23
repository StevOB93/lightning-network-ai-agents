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
python -m pytest ai/tests/test_llm_adapters.py -v               # single file
python -m pytest ai/tests/test_executor.py::TestExecutor         # single class
```

### Web UI Server
```bash
cd ln-ai-network
python scripts/ui_server.py   # serves web/ at http://127.0.0.1:8008 with SSE streaming
```

### Run Agent Offline (Mock Mode)
```bash
cd ln-ai-network
python -m ai.agent   # runs against mock fixtures in ai/mocks/fixtures/
```

### Run Pipeline Mode
```bash
cd ln-ai-network
python -m ai.pipeline   # 4-stage pipeline: Translator → Planner → Executor → Summarizer
```

## Architecture

### Data Flow (Pipeline Mode — primary)
```
User prompt → inbox.jsonl → PipelineCoordinator
    │
    ├─ 1. Translator  (LLM) → IntentBlock (goal, intent_type, context)
    ├─ 2. Planner     (LLM) → ExecutionPlan (ordered PlanSteps with dependencies)
    ├─ 3. Executor    (MCP) → StepResults (tool calls with results)
    └─ 4. Summarizer  (LLM) → human-readable report
    │
    └→ outbox.jsonl → Web UI (SSE)
```

### Data Flow (Legacy Agent Mode)
```
User → inbox.jsonl → LightningAgent → ConversationController → LLM → tool_calls → MCP → bitcoin-cli / lightning-cli
                                                                                            ↓
User ← outbox.jsonl ← Agent ← structured intent JSON ←──────────────────────── tool results
```

### Core Components

- **`ai/pipeline.py`** — PipelineCoordinator: top-level orchestrator for the 4-stage pipeline. Each stage uses a separate LLM backend instance (via `create_backend_for_role()`). Supports multi-turn history, goal verification, and SSE streaming.

- **`ai/agent.py`** — Legacy single-agent mode. Thin process shell (~230 lines) that delegates to `ConversationController`. Build tag: `clean-single-agent-v5`.

- **`ai/models.py`** — Shared data models: `IntentBlock` (Translator output), `ExecutionPlan`/`PlanStep` (Planner output), `StepResult` (Executor output), `PipelineResult` (full run record).

- **`ai/tools.py`** — Centralized MCP tool registry, normalization, schema generation. Defines `READ_ONLY_TOOLS`, `STATE_CHANGING_TOOLS`, `TOOL_REQUIRED` arg specs, `_normalize_tool_args()`, `_is_tool_error()`, `_tool_sig()`, and `llm_tools_schema()`.

- **`ai/controllers/`** — Pipeline stage implementations:
  - `translator.py` — NL prompt → IntentBlock via LLM
  - `planner.py` — IntentBlock → ExecutionPlan via LLM
  - `executor.py` — ExecutionPlan → StepResults via MCP tool calls (supports parallel execution with dependency ordering)
  - `summarizer.py` — Tool results → human-readable answer via LLM
  - `conversation.py` — Multi-turn LLM+MCP conversation loop (legacy agent mode)
  - `shared.py` — Shared controller utilities

- **`ai/llm/`** — LLM backend abstraction. All three adapters now conform to the `LLMBackend` ABC (`step(LLMRequest) → LLMResponse`):
  - `base.py` — `LLMBackend` ABC, `LLMRequest`, `LLMResponse`, error taxonomy (`AuthError`, `RateLimitError`, `TransientAPIError`, `PermanentAPIError`)
  - `factory.py` — `create_backend()` + `create_backend_for_role()` with lazy imports and per-stage model config
  - `adapters/ollama_backend.py` — local Ollama (default)
  - `adapters/openai_backend.py` — OpenAI API (+ streaming support)
  - `adapters/gemini_backend.py` — Google Gemini (converts OpenAI format ↔ Gemini format)
  - `guarded_backend.py` — Decorator adding rate limiting, exponential backoff, circuit breaker, and concurrency gating

- **`ai/core/`** — Infrastructure utilities (now wired into the pipeline via `GuardedBackend`):
  - `backoff.py` — Deterministic exponential backoff + circuit breaker
  - `rate_limiter.py` — Dual RPM/TPM rate limiter
  - `concurrency.py` — Concurrency gate (semaphore)
  - `config.py` — `AgentConfig` (reads all config from env vars)
  - `token_estimation.py` — Heuristic token counter
  - `registry.py` — `AgentRegistry` for multi-agent coordination
  - `scheduler.py` — Deterministic scheduler

- **`ai/mcp_client.py`** — `MCPClient` protocol + `FixtureMCPClient` (deterministic mock) + `FastMCPClientWrapper` (thread-safe with timeout via `MCPTimeoutError`).

- **`ai/utils.py`** — `StartupLock`, `TraceLogger`, env helpers.

- **`ai/command_queue.py`** — File-based JSONL queue with byte-offset tracking for deterministic reads.

- **`mcp/ln_mcp_server.py`** — MCP server exposing tools (bitcoin: `btc_*`, lightning: `ln_*`, health: `network_health`). All tools call `bitcoin-cli` or `lightning-cli` via subprocess.

- **`scripts/ui_server.py`** — Full pipeline dashboard web server with SSE streaming, network graph, trace log, and prompt input. Replaces the earlier `demo_ui_server.py`.

- **`web/`** — Front-end dashboard (vanilla HTML/CSS/JS). Polls `/api/status` and receives SSE events for live pipeline state.

### Boot Sequence (scripts/startup/)
1. `0.1.infra_boot.sh` — bitcoind + regtest chain
2. `0.2.control_plane_boot.sh` — MCP server + agent control plane
3. `0.3.agent_boot.sh` — AI agent process
4. `0.4.ui_server.sh` — Web UI server

Shutdown reverses this order via `scripts/shutdown/`.

## Key Conventions

- **MCP boundary**: The AI agent never executes shell commands directly. All actions go through MCP tools. The agent reads via MCP and outputs structured intent JSON only.
- **regtest only**: All Bitcoin/Lightning operations run on regtest. Never mainnet.
- **Secrets in `.env`**: Real API keys go in `ln-ai-network/.env` (gitignored). Use `.env.example` as template. `env.sh` warns if key is missing or still placeholder.
- **LLM backend toggle**: Set `LLM_BACKEND=ollama` (default), `openai`, or `gemini` in `.env`. Per-stage override: `TRANSLATOR_LLM_BACKEND`, `PLANNER_LLM_BACKEND`, etc. Per-stage model override: `TRANSLATOR_OLLAMA_MODEL`, etc.
- **Deterministic design**: Trace logging, byte-offset queue cursors, deterministic jitter (not random), signature-based dedup for tool calls.
- **Fail-fast on tool errors**: If an MCP tool returns an error, the executor marks the step as failed. Abort vs skip is controlled per-step via `on_error`.
- **Runtime state is ephemeral**: The `runtime/` directory (blockchain data, node configs, agent logs) is gitignored and recreated by scripts.
- **Mock fixtures**: Test without live infrastructure using JSON fixtures in `ai/mocks/fixtures/`.
- **CI**: GitHub Actions runs `pytest ai/tests/` on every push to `main` and on PRs. All tests must pass without API keys or live infrastructure.
- **Commit directly to main**: This project commits directly to `main`. Do not create feature branches or PRs unless explicitly asked.
