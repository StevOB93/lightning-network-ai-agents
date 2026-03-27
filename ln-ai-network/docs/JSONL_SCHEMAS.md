# JSONL File Schemas

The system uses append-only JSONL (JSON Lines) files for all inter-process communication and persistence. Each file contains one JSON object per line. This document describes the schema for each file.

All files live under `runtime/agent/` (single-agent mode) or `runtime/agent-{N}/` (multi-agent mode).

## inbox.jsonl

Commands written by the UI or CLI, consumed by the pipeline agent.

**Location:** `runtime/agent/inbox.jsonl`

```json
{
  "id": 42,
  "ts": 1710871234,
  "role": "user",
  "content": "What is the balance of node 1?",
  "meta": {
    "kind": "freeform",
    "use_llm": true,
    "strategy": "conservative"
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `id` | int | Monotonically increasing message ID (from `msg.counter`) |
| `ts` | int | Unix timestamp (seconds) |
| `role` | string | Always `"user"` |
| `content` | string | The raw prompt or command text |
| `meta.kind` | string | `"freeform"` (NL prompt) or `"health_check"` |
| `meta.use_llm` | bool | Whether the pipeline should invoke the LLM |
| `meta.strategy` | string | Optional strategy profile name |

### Routed messages (multi-agent)

When one agent routes a query to another, the inbox message includes extra fields:

```json
{
  "id": 42,
  "ts": 1710871234,
  "role": "user",
  "content": "What is node 2's balance?",
  "meta": { "kind": "freeform", "use_llm": true },
  "reply_id": "route-42-a1b2c3d4",
  "reply_inbox": "/path/to/agent-1/inbox.jsonl",
  "routed_from_node": 1
}
```

| Field | Type | Description |
|-------|------|-------------|
| `reply_id` | string | Correlation ID for matching the reply |
| `reply_inbox` | string | Absolute path to the sender's inbox |
| `routed_from_node` | int | Node number of the sending agent |

## outbox.jsonl

Pipeline results written by the agent, consumed by the UI server.

**Location:** `runtime/agent/outbox.jsonl`

```json
{
  "ts": 1710871240,
  "type": "pipeline_report",
  "request_id": 42,
  "success": true,
  "stage_failed": null,
  "intent": {
    "goal": "Check node 1 balance",
    "intent_type": "freeform",
    "context": {},
    "success_criteria": [],
    "clarifications_needed": [],
    "human_summary": "Checking balance.",
    "raw_prompt": "what is the balance of node 1?"
  },
  "plan": {
    "steps": [
      { "tool": "ln_listfunds", "args": { "node": 1 }, "on_error": "abort" }
    ]
  },
  "step_results": [
    {
      "tool": "ln_listfunds",
      "args": { "node": 1 },
      "ok": true,
      "result": { "payload": { "outputs": [...], "channels": [...] } },
      "error": null
    }
  ],
  "content": "Node 1 has 500,000 sat across 2 outputs.",
  "error": null,
  "pipeline_build": "pipeline-v1(translator+planner+executor+summarizer+history+verify)"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `type` | string | Always `"pipeline_report"` |
| `request_id` | int | Matches the inbox message `id` |
| `success` | bool | True if all required steps completed |
| `stage_failed` | string? | `"translator"`, `"planner"`, `"executor"`, `"router"`, or `null` |
| `intent` | object? | Parsed IntentBlock, `null` if translator failed |
| `plan` | object? | Execution plan, `null` if planner failed or skipped |
| `step_results` | array | List of executed step results (partial on failure) |
| `content` | string | Human-readable answer (aliased from `human_summary`) |
| `error` | string? | Error message on failure, `null` on success |
| `pipeline_build` | string | Pipeline version string |

## trace.log

Structured trace events emitted by each pipeline stage. Used by the UI for live monitoring.

**Location:** `runtime/agent/trace.log`

Each line is a JSON object with at least `ts` and one of `kind`, `event`, or `stage`:

### Common trace events

```json
{"ts": 1710871234, "event": "prompt_start", "req_id": 42, "user_text": "...", "build": "..."}
{"ts": 1710871235, "event": "stage_timing", "req_id": 42, "translator_ms": 1200.5, "planner_ms": 800.2}
{"ts": 1710871236, "event": "stage_failed", "stage": "executor", "error": "MCP timeout"}
{"ts": 1710871237, "event": "goal_verify", "req_id": 42, "tool": "ln_listfunds", "ok": true}
{"ts": 1710871238, "event": "goal_verify_failed", "req_id": 42, "tool": "ln_listfunds", "error": "..."}
```

### Routing trace events (multi-agent)

```json
{"ts": 1710871234, "event": "route_send", "req_id": 42, "target_node": 2, "reply_id": "route-42-a1b2c3d4", "routed_prompt": "..."}
{"ts": 1710871240, "event": "route_reply", "req_id": 42, "reply_id": "route-42-a1b2c3d4", "received": true}
{"ts": 1710871240, "event": "route_reply_sent", "reply_id": "route-42-a1b2c3d4"}
{"ts": 1710871240, "event": "route_reply_failed", "reply_id": "route-42-a1b2c3d4", "error": "..."}
```

### Token streaming events

```json
{"ts": 1710871234, "event": "stream_start", "req_id": 42}
{"ts": 1710871235, "event": "token", "text": "Node 1 has "}
{"ts": 1710871236, "event": "stream_end", "req_id": 42}
```

## history.jsonl

Rolling conversation history for the Translator's context window. Automatically compacted on load when it exceeds `max_history_messages * 2` lines.

**Location:** `runtime/agent/history.jsonl`

```json
{"role": "user", "content": "What is the balance of node 1?"}
{"role": "assistant", "content": "Check node 1 balance"}
{"role": "user", "content": "Open a channel from node 1 to node 2"}
{"role": "assistant", "content": "Open a 500k sat channel from node 1 to node 2"}
```

| Field | Type | Description |
|-------|------|-------------|
| `role` | string | `"user"` or `"assistant"` |
| `content` | string | User's raw prompt or the assistant's goal string |

Messages are stored in pairs (user + assistant). The assistant content is the intent's `goal` string, not the full verbose summary, to keep the context compact. Duplicate consecutive exchanges (same user text + same goal) are deduplicated.

## archive.jsonl

Permanent episodic archive. Never trimmed — grows as an audit log of all queries.

**Location:** `runtime/agent/archive.jsonl`

```json
{
  "ts": 1710871240,
  "user": "What is the balance of node 1?",
  "goal": "Check node 1 balance",
  "outcome": "ok",
  "summary": "Node 1 has 500,000 sat across 2 outputs."
}
```

| Field | Type | Description |
|-------|------|-------------|
| `ts` | int | Unix timestamp |
| `user` | string | Original user prompt |
| `goal` | string | Intent goal extracted by the Translator |
| `outcome` | string | `"ok"`, `"partial"`, or `"failed"` |
| `summary` | string | Human-readable summary of the result |

## Archived trace files

Per-query trace archives stored in `runtime/agent/logs/`.

**Naming convention:** `{req_id:04d}_{YYYYMMDD-HHMMSS}_{status}.jsonl`

Example: `0042_20260320-143022_ok.jsonl`

Each file contains the same event format as `trace.log`, scoped to a single query.

## Cursor files

| File | Content | Purpose |
|------|---------|---------|
| `inbox.offset` | Single integer (byte offset) | Read cursor into inbox.jsonl |
| `msg.counter` | Single integer | Next message ID to assign |
| `pipeline.lock` | `pid=NNN started_ts=NNN` | Singleton lock for the pipeline process |

## registry.jsonl (multi-agent)

Agent registry for inter-agent routing.

**Location:** `runtime/registry.jsonl`

```json
{"kind": "pipeline", "node": 1, "pid": 12345, "inbox": "/path/to/agent-1/inbox.jsonl", "ts": 1710871234}
{"kind": "pipeline", "node": 2, "pid": 12346, "inbox": "/path/to/agent-2/inbox.jsonl", "ts": 1710871235}
```

| Field | Type | Description |
|-------|------|-------------|
| `kind` | string | Always `"pipeline"` |
| `node` | int | Node number this agent owns |
| `pid` | int | OS process ID (used for liveness checking) |
| `inbox` | string | Absolute path to this agent's inbox.jsonl |
| `ts` | int | Registration timestamp |

Dead entries (PID no longer running) are filtered out by `list_peers()` and cleaned up by `purge_stale()`.
