# AI / Pipeline / Control Plane

The AI subsystem is a **4-stage pipeline** that converts natural language prompts into Lightning Network actions, executed exclusively via MCP tools.

**Boundary:** the agent may ONLY act via MCP tools — no direct shell access, no subprocess spawning, no file writes.

---

## Architecture

```
User prompt (inbox.jsonl)
        │
        ▼
 ┌─────────────┐
 │  Translator  │  LLM call → IntentBlock
 │  (Stage 1)  │  goal, intent_type, context, success_criteria
 └──────┬──────┘
        │
        ▼
 ┌─────────────┐
 │   Planner   │  LLM call → ExecutionPlan
 │  (Stage 2)  │  ordered PlanSteps with tool/args/$step placeholders
 └──────┬──────┘
        │
        ▼
 ┌─────────────┐
 │  Executor   │  MCP calls → List[StepResult]
 │  (Stage 3)  │  retry/skip/abort per step, placeholder resolution
 └──────┬──────┘
        │
        ▼
 ┌─────────────┐
 │  Summarizer │  LLM call → human-readable answer
 │  (Stage 4)  │  synthesises tool results into a clear response
 └──────┬──────┘
        │
        ▼
 PipelineResult → outbox.jsonl + Web UI (SSE)
```

Each stage uses an **independently configured** LLM backend (model, temperature, token budget) via role-specific env vars. Changing the Planner model does not affect the Translator or Summarizer.

---

## Stage details

### Stage 1 — Translator

Converts raw natural language into a structured `IntentBlock` using a low-temperature LLM call.

**Output fields:**
- `goal` — one-sentence machine-readable statement of what the user wants
- `intent_type` — one of: `open_channel`, `set_fee`, `rebalance`, `pay_invoice`, `noop`, `freeform`
- `context` — extracted entities (node numbers, amounts, bolt11 strings, labels)
- `success_criteria` — list of observable conditions that would confirm success
- `clarifications_needed` — any ambiguous values that prevent execution
- `human_summary` — friendly confirmation of what was understood

**Retry logic:** on JSON parse failure the error is appended to the conversation so the LLM can self-correct (up to `TRANSLATOR_MAX_RETRIES` additional attempts, default 2).

**Safety gate:** `intent_validate.py` checks the parsed intent for forbidden patterns (shell metacharacters, path traversal, HTTP URLs, sudo commands) before the intent is passed to the Planner.

---

### Stage 2 — Planner

Converts an `IntentBlock` into an ordered `ExecutionPlan` via an LLM call.

**Output:** a list of `PlanStep` objects, each containing:
- `step_id` — integer, used for `$step1.result.payload.field` placeholder references
- `tool` — MCP tool name (validated against the known tool registry)
- `args` — dict of arguments, may include `$stepN.result.payload.X` placeholders
- `error_policy` — `retry` (default), `skip`, or `abort`
- `rationale` — one-sentence explanation of why this step is needed

**Placeholder chaining:** the Planner can reference outputs from earlier steps. For example, `ln_invoice` in step 1 produces a bolt11 string, and `ln_pay` in step 2 can reference `$step1.result.payload.bolt11`. The Executor resolves these at runtime before each call.

**Tool guidance:** the Planner's system prompt includes a complete tool reference table (names, required args, descriptions) and explicit rules for common patterns (channel opens, payments, diagnostics).

---

### Stage 3 — Executor

Runs each `PlanStep` against the MCP server in order.

**Per-step behaviour:**
- Resolves `$stepN.result.payload.X` placeholders by walking the result tree of prior steps
- Normalizes args (coerces string integers, unwraps nested `{"args": {...}}` patterns, validates node numbers against `runtime/node_count`)
- On tool error: applies the step's `error_policy` — retry up to N times, skip the step, or abort the plan
- Detects oscillation: a read-only tool called with identical args since the last state change is blocked after `MAX_CONSEC_READ_ONLY` consecutive read-only calls

**Goal verification:** after a state-changing intent (`pay_invoice`, `open_channel`, `rebalance`, `set_fee`) completes, the Executor makes one additional read-only MCP call to confirm the state change actually occurred (e.g. `ln_listchannels` after `open_channel`).

---

### Stage 4 — Summarizer

Takes the full set of `StepResult` objects and synthesises a concise human-readable answer via an LLM call.

The Summarizer is the only stage whose output is shown directly to the user. It extracts the most relevant information from potentially verbose tool output (e.g. a full channel list) and presents it in plain language.

---

## Multi-turn conversation history

The last `PIPELINE_HISTORY_MAX` (default: 4) prompt/response pairs are injected into the Translator's message list as prior conversation turns. This enables follow-up prompts like "now do the same for node 2" or "pay that invoice" to resolve references to the prior response without re-stating context.

History is persisted to `runtime/agent/history.jsonl` and survives agent restarts.

---

## Files

| File | Purpose |
|------|---------|
| `pipeline.py` | Main coordinator — event loop, stage orchestration, history, goal verification |
| `agent.py` | Legacy monolithic agent (kept for reference; `pipeline.py` is the active entry point) |
| `models.py` | Frozen dataclasses: `IntentBlock`, `ExecutionPlan`, `PlanStep`, `StepResult`, `PipelineResult` |
| `tools.py` | Centralized tool registry, arg normalization, schema generation, oscillation detection |
| `command_queue.py` | `inbox.jsonl` / `outbox.jsonl` read/write with file locking and cursor tracking |
| `mcp_client.py` | MCP tool call interface — wraps `FastMCPClient` with timeout and retry logic |
| `intent_validate.py` | Safety gate: rejects intents containing shell metacharacters, path traversal, URLs, etc. |
| `core/config.py` | `AgentConfig` — all pipeline env vars, read once at startup |
| `core/registry.py` | `AgentRegistry` — in-memory store for pipeline state shared across stages |
| `core/scheduler.py` | `DeterministicScheduler` — tick-based inbox polling loop |
| `utils.py` | `TraceLogger`, `StartupLock`, env helpers, path resolution |
| `llm/factory.py` | Creates LLM backends (`ollama` / `openai` / `gemini`) per role |
| `llm/guarded_backend.py` | `GuardedBackend` — enforces `ALLOW_LLM` flag, wraps any backend |
| `llm/adapters/ollama.py` | Ollama HTTP adapter |
| `llm/adapters/openai.py` | OpenAI / compatible API adapter |
| `llm/adapters/gemini.py` | Google Gemini adapter |
| `controllers/translator.py` | Stage 1: text → `IntentBlock` via LLM, with JSON repair and retry |
| `controllers/planner.py` | Stage 2: `IntentBlock` → `ExecutionPlan` via LLM |
| `controllers/executor.py` | Stage 3: MCP execution, placeholder resolution, retry/skip/abort |
| `controllers/summarizer.py` | Stage 4: tool results → human answer via LLM |
| `controllers/shared.py` | Shared utilities: env readers, `_repair_json`, `_strip_code_fences`, `_get_node_count` |

---

## Running

```bash
# Via the full system launcher (recommended — starts infra + MCP + agent + UI)
./scripts/1.start.sh 2

# Agent only (requires infra and MCP server already running)
source .venv/bin/activate
python -m ai.pipeline

# Restart the agent without touching infra
./scripts/restart_agent.sh
./scripts/restart_agent.sh fresh   # also clears inbox/outbox/cursor
```

---

## Sending prompts (CLI)

```bash
# Enqueue a prompt directly
python3 -c "
from ai.command_queue import enqueue
enqueue('open a channel from node 1 to node 2 with 500000 sat')
"

# Watch the result
tail -n 1 runtime/agent/outbox.jsonl | python3 -m json.tool

# Watch the live trace
tail -f runtime/agent/trace.log | python3 -m json.tool
```

---

## Tests

```bash
source .venv/bin/activate
pytest ai/tests/ -v
# 205 tests covering all four pipeline stages, JSON repair, tool validation,
# safety gates, history, goal verification, and integration scenarios.
```

---

## Environment variables

### LLM backend selection

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_BACKEND` | `openai` | Backend for all stages: `openai`, `ollama`, `gemini` |
| `TRANSLATOR_LLM_BACKEND` | `LLM_BACKEND` | Override backend for Translator stage |
| `PLANNER_LLM_BACKEND` | `LLM_BACKEND` | Override backend for Planner stage |
| `SUMMARIZER_LLM_BACKEND` | `LLM_BACKEND` | Override backend for Summarizer stage |
| `ALLOW_LLM` | `1` | Set to `0` to block all LLM calls (dry-run / test mode) |

### LLM provider credentials

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_API_KEY` | — | Required when using OpenAI backend |
| `OPENAI_MODEL` | `gpt-4o-mini` | OpenAI model name |
| `GEMINI_API_KEY` | — | Required when using Gemini backend |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Ollama server endpoint |
| `OLLAMA_MODEL` | — | Ollama model (e.g. `llama3`, `mistral`) |

### Pipeline tuning

| Variable | Default | Description |
|----------|---------|-------------|
| `PIPELINE_HISTORY_MAX` | `4` | Prior exchanges passed to Translator as conversation context |
| `TRANSLATOR_MAX_RETRIES` | `2` | Additional attempts after a JSON parse failure |
| `TRANSLATOR_TEMPERATURE` | `0.1` | Translator LLM temperature (low = deterministic) |
| `TRANSLATOR_MAX_OUTPUT_TOKENS` | `512` | Translator output token budget |
| `PLANNER_TEMPERATURE` | `0.2` | Planner LLM temperature |
| `PLANNER_MAX_OUTPUT_TOKENS` | `1024` | Planner output token budget |
| `MCP_CALL_TIMEOUT_S` | `30` | Timeout per MCP tool call |
| `PIPELINE_POLL_INTERVAL_S` | `1` | Inbox polling interval in seconds |
