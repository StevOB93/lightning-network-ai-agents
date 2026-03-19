# LN AI Network — Prompt-Driven Lightning Agent (Regtest)

A **3-stage AI pipeline** that autonomously completes Lightning Network workflows on regtest. Type a prompt — the agent translates it into a structured intent, plans a sequence of MCP tool calls, and executes them.

**Strict boundary: the agent may ONLY act via MCP tools. No direct shell access.**

---

## Quick Start

### 1. Install

```bash
cd ln-ai-network
./scripts/0.install.sh
cp .env.example .env
# Edit .env: set OPENAI_API_KEY (or configure Ollama/Gemini), set ALLOW_LLM=1
```

### 2. Start the full system

```bash
./scripts/1.start.sh 2
```

This starts:
- Bitcoin (regtest) + Core Lightning nodes
- The AI pipeline (`ai/pipeline.py`)
- The web UI server at **http://127.0.0.1:8008**

### 3. Open the web UI

Navigate to `http://127.0.0.1:8008` and type a prompt in the input box.

Or use the CLI:

```bash
source .venv/bin/activate
python -c "from ai.command_queue import enqueue; enqueue('check network health', meta={'kind':'freeform','use_llm':True})"
tail -n 1 runtime/agent/outbox.jsonl | python3 -m json.tool
```

---

## Pipeline Architecture

```
Prompt → [Translator] → IntentBlock → [Planner] → ExecutionPlan → [Executor] → Results
```

| Stage | Input | Output | LLM? |
|-------|-------|--------|------|
| Translator | Raw text | IntentBlock (goal, intent_type, context) | Yes |
| Planner | IntentBlock | ExecutionPlan (ordered tool steps) | Yes |
| Executor | ExecutionPlan | List[StepResult] (per-tool results) | No (MCP only) |

**Features:**
- Multi-turn conversation — last 4 exchanges carried as context for follow-up prompts
- Goal verification — after state-changing intents, a read-only MCP call confirms success
- Retry / skip / abort error policies per step
- `$step1.result.payload.bolt11` placeholder chaining between steps
- Per-stage LLM backend config (mix Ollama for planning, OpenAI for translation, etc.)

---

## LLM Backends

| Backend | Env var | Requires |
|---------|---------|---------|
| Ollama (local) | `LLM_BACKEND=ollama` | Ollama running locally |
| OpenAI | `LLM_BACKEND=openai` | `OPENAI_API_KEY` |
| Gemini | `LLM_BACKEND=gemini` | `GEMINI_API_KEY` |

Per-stage overrides: `TRANSLATOR_LLM_BACKEND`, `PLANNER_LLM_BACKEND`

---

## Web UI

The dashboard at `http://127.0.0.1:8008` shows:
- **Prompt input** — submit any Lightning Network instruction
- **Pipeline stage cards** — Translator → IntentBlock, Planner → plan steps, Executor → per-step results
- **Network graph** — D3 force-directed visualization of nodes and channels (auto-populated from tool results)
- **Live trace log** — real-time event stream via Server-Sent Events
- **Inbox / Outbox** — message history

---

## Running Tests

```bash
cd ln-ai-network
source .venv/bin/activate
python -m pytest ai/tests/ -v
```

---

## Logs

| File | Contents |
|------|---------|
| `runtime/agent/trace.log` | Per-prompt trace (resets each request) |
| `runtime/agent/outbox.jsonl` | Pipeline results |
| `logs/system/0.3.agent_boot.log` | Pipeline process log |
| `logs/system/0.4.ui_server.log` | Web UI server log |

---

## End-to-End Payment Prompt (Regtest)

```
Open a 500,000 sat channel from node 1 to node 2, then have node 2 create an
invoice for 10,000 msat and pay it from node 1.
```

Paste this into the web UI or CLI. The agent will plan and execute every step.

---

## Project Layout

```
ln-ai-network/
├── ai/                    # Pipeline, controllers, LLM backends
│   ├── pipeline.py        # Main coordinator
│   ├── controllers/       # translator, planner, executor
│   ├── llm/               # factory + adapters (ollama, openai, gemini)
│   ├── models.py          # Pipeline data structures
│   ├── tools.py           # Tool registry and normalization
│   └── tests/             # Unit + integration tests
├── mcp/                   # MCP tool server (bitcoin-cli / lightning-cli)
├── scripts/               # Start, install, startup sequence
│   └── ui_server.py       # Web UI HTTP server
├── web/                   # Frontend (HTML, JS, CSS)
└── runtime/               # Created at runtime (inbox, outbox, logs)
```
