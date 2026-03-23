---
title: AI Agent Research
---

# AI Agent

The AI agent is a prompt-driven controller that translates natural language instructions into Lightning Network operations. It acts exclusively through MCP tools — no shell access, no direct file I/O to the Bitcoin/Lightning processes.

## How it works

```
User prompt
     │
     ▼
[Translator] — LLM call
     │  Outputs: IntentBlock
     │  { goal, intent_type, context, success_criteria }
     ▼
[Planner] — LLM call
     │  Outputs: ExecutionPlan
     │  [ { tool, args, rationale }, ... ]
     ▼
[Executor] — no LLM
     │  Runs each MCP tool call
     │  Chains output values across steps
     │  Verifies goal after state-changing ops
     ▼
[Summarizer] — LLM call
     │  Outputs: human-readable answer + success/failure
     ▼
Answer displayed in web UI
```

## Key properties

**Tool-only execution.** The agent can call exactly the 22 tools exposed by the MCP server. It cannot run shell commands, read arbitrary files, or interact with any other system.

**Structured intermediate state.** Each stage produces a typed Pydantic model (`IntentBlock`, `ExecutionPlan`, `StepResult`). Parsing failures trigger a repair pass before falling back to an error.

**Multi-turn context.** The last N prompt/result pairs are included in LLM calls. This allows follow-up prompts like "now pay that invoice" or "what was the balance change?" to work naturally.

**Value chaining.** The Executor extracts output values from tool results (e.g., the `bolt11` invoice string from `ln_invoice`) and passes them as arguments to subsequent tool calls, without requiring the LLM to copy values through.

**Goal verification.** After payment or channel operations, the Executor automatically runs a read-only check (e.g., `ln_listfunds`) to confirm the action took effect.

## Source layout

| File | Purpose |
|------|---------|
| `ai/pipeline.py` | Main loop: inbox polling, stage orchestration, outbox writing |
| `ai/agent.py` | LLM client: streaming, tool use, multi-turn, backend routing |
| `ai/models.py` | Pydantic models for all structured pipeline state |
| `ai/tools.py` | Tool schemas (for LLM prompts), `TOOL_REQUIRED`, `READ_ONLY_TOOLS` |
| `ai/controllers/translator.py` | Stage 1 |
| `ai/controllers/planner.py` | Stage 2 |
| `ai/controllers/executor.py` | Stage 3 |
| `ai/controllers/summarizer.py` | Stage 4 |
| `ai/controllers/shared.py` | JSON repair, parsing helpers |
| `ai/mcp_client.py` | JSON-RPC client to `ln_mcp_server.py` |

## LLM backends

| Backend | `LLM_BACKEND` | Notes |
|---------|--------------|-------|
| OpenAI | `openai` | Default. Requires `OPENAI_API_KEY`. |
| Ollama | `ollama` | Local, free. Requires Ollama running and a pulled model. |
| Gemini | `gemini` | Requires `GEMINI_API_KEY`. |

Set `LLM_TEMPERATURE=0` for deterministic, reproducible tool-calling behavior.

## Running tests

```bash
cd ln-ai-network
source .venv/bin/activate
python -m pytest ai/tests/ -v
```

All 205+ tests run offline (no LLM calls, no Lightning infrastructure needed).

## Related

- [MCP & A2A overview](mcp-a2a.md)
- [MCP Tools reference](../3.Components/TOOLS.md)
- [System design](../2.Architecture/system-design.md)
