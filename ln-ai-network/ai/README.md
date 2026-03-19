# AI / Pipeline / Control Plane

The AI subsystem is a **3-stage pipeline** that converts natural language prompts into Lightning Network actions, executed exclusively via MCP tools.

**Boundary:** the agent may ONLY act via MCP tools — no direct shell access.

---

## Architecture

```
User prompt (inbox.jsonl)
        │
        ▼
 ┌─────────────┐
 │  Translator  │  LLM call → IntentBlock
 │ (Stage 1)   │  goal, intent_type, context, success_criteria
 └──────┬──────┘
        │
        ▼
 ┌─────────────┐
 │   Planner   │  LLM call → ExecutionPlan
 │ (Stage 2)   │  ordered PlanSteps with tool/args/$step placeholders
 └──────┬──────┘
        │
        ▼
 ┌─────────────┐
 │  Executor   │  MCP calls → List[StepResult]
 │ (Stage 3)   │  retry/skip/abort per step, placeholder resolution
 └──────┬──────┘
        │
        ▼
 PipelineResult → outbox.jsonl + Web UI
```

Each stage has role-specific LLM backend configuration via env vars.

---

## Files

| File | Purpose |
|------|---------|
| `pipeline.py` | Main coordinator/event loop — runs the 3 stages per inbox message |
| `controllers/translator.py` | Stage 1: text → IntentBlock via LLM |
| `controllers/planner.py` | Stage 2: IntentBlock → ExecutionPlan via LLM |
| `controllers/executor.py` | Stage 3: MCP execution with placeholder resolution and retry/skip/abort |
| `models.py` | Frozen dataclasses: IntentBlock, ExecutionPlan, PlanStep, StepResult, PipelineResult |
| `tools.py` | Centralized tool registry (38 tools), arg normalization, schema generation |
| `llm/factory.py` | Creates LLM backends (ollama / openai / gemini), per-role config |
| `llm/adapters/` | Backend adapters: ollama, openai, gemini |
| `command_queue.py` | inbox.jsonl / outbox.jsonl read/write with file locking |
| `mcp_client.py` | MCP tool call interface |
| `intent_validate.py` | Safety gate for parsed intents |
| `agent.py` | Legacy monolithic agent (kept for reference, no longer the entry point) |

---

## Features

- **Multi-turn conversation**: the last 4 prompt/response pairs are passed to the Translator as context, enabling follow-up prompts ("now pay that invoice")
- **Goal verification**: after state-changing intents (pay_invoice, open_channel, rebalance), a read-only MCP call confirms the state change occurred
- **Retry / skip / abort policies**: each PlanStep declares its error policy; the Executor enforces it
- **Placeholder resolution**: `$step1.result.payload.bolt11` chains output from one step into the next
- **Per-stage LLM backends**: Translator and Planner can use different models/providers

---

## Running

```bash
# Via the full system start (recommended)
./scripts/1.start.sh 2

# Or standalone
source .venv/bin/activate
python -m ai.pipeline
```

---

## Web UI

A web dashboard is available at `http://127.0.0.1:8008` after startup.

It shows real-time pipeline stage results, the network graph, and a live trace log. Prompts can be submitted directly from the UI.

```bash
python -m scripts.ui_server
```

---

## Tests

```bash
source .venv/bin/activate
python -m pytest ai/tests/ -v
```

43 unit tests + integration tests covering all three pipeline stages.

---

## Sending prompts (CLI)

```bash
# Via the command queue directly
python -c "from ai.command_queue import enqueue; enqueue('check network health', meta={'kind':'freeform','use_llm':True})"

# Check the result
tail -n 1 runtime/agent/outbox.jsonl | python3 -m json.tool
```

---

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_BACKEND` | `ollama` | Backend for all stages: `ollama`, `openai`, `gemini` |
| `TRANSLATOR_LLM_BACKEND` | — | Override backend for Translator stage |
| `PLANNER_LLM_BACKEND` | — | Override backend for Planner stage |
| `PIPELINE_HISTORY_MAX` | `4` | Number of prior exchanges to include as conversation context |
| `ALLOW_LLM` | `0` | Must be `1` to enable LLM calls |
| `OPENAI_API_KEY` | — | Required if using OpenAI backend |
| `GEMINI_API_KEY` | — | Required if using Gemini backend |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Ollama endpoint |
