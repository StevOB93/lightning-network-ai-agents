# Web UI

A real-time single-page dashboard for the Lightning Network AI pipeline. Served by `scripts/ui_server.py` at `http://127.0.0.1:8008` (or your configured `UI_HOST:UI_PORT`).

---

## Tabs

### Agent tab (default)

The primary interaction surface.

- **Prompt input** — type a natural language command and press Enter to submit (Shift+Enter for a newline). The prompt is POSTed to `/api/ask` and written to the agent's inbox queue.
- **Summary card** — shows the pipeline's answer once it completes: the human-readable response from the Summarizer, a ✓/✗ success indicator, and a timestamp.
- **Health check** — the Health button sends a diagnostic ping to verify the agent is reachable and the Lightning network is operational.

### Pipeline tab

Live view of the 4-stage pipeline execution for the most recent request:

- **Translator card** — the parsed `IntentBlock`: goal, intent type, extracted context, success criteria
- **Planner card** — the `ExecutionPlan`: each step's tool name, arguments, and rationale
- **Executor card** — step-by-step results: tool called, arguments sent, result summary, success/failure

Stage cards are color-coded: green border = succeeded, red = failed, grey = not yet run. Stage badges show `ok`, `fail`, `running`, or `—`.

### Network tab

A live D3 force-directed graph of the Lightning Network topology:

- **Nodes** — each Lightning node appears as a circle labeled with its alias and truncated pubkey
- **Channels** — payment channels appear as edges; channel capacity is encoded in edge width
- **Hover** — shows node or channel details
- **Refresh** — fetches the latest topology from `/api/network` (calls `ln_listchannels` on all nodes)

### Logs tab

**Live trace** — a streaming event log showing every pipeline event in real time:
- `prompt_start`, `llm_call`, `llm_response`, `parse_failed` — Translator events
- `intent_parsed`, `plan_parsed` — structured output events
- `step_start`, `tool_call`, `tool_result` — Executor events
- `stage_timing` — per-stage and total latency
- Copy button (⎘) — copies the full trace to the clipboard
- Archive button — opens the archive panel showing past pipeline runs

**Archive panel** — collapses/expands a list of completed pipeline run log files. Click any entry to see its full trace inline.

**Inbox** — the last 10 entries written to `runtime/agent/inbox.jsonl` (incoming commands). Shows request ID, timestamp, and a preview of the command body. Clear button resets the display (client-side; does not modify the file).

**Outbox** — the last 10 entries written to `runtime/agent/outbox.jsonl` (pipeline results). Shows request ID, timestamp, and a preview of the result. Clear button resets the display.

**Crash Kit** — one-click debug snapshot. Clicking "Copy Crash Kit" fetches `/api/crash_kit`, formats a plain-text report containing system info, non-sensitive config, runtime status, the last pipeline result, recent trace events, and metrics — then copies it to the clipboard. Paste into a bug report.

### Settings tab

A grouped configuration form for common runtime settings. Changes are written to `.env` via `/api/config` and take effect on the next agent restart (pipeline settings) or immediately (UI settings). Groups:

- **LLM** — `LLM_BACKEND`, `OPENAI_MODEL`, `OLLAMA_MODEL`, `OLLAMA_BASE_URL`
- **Timeouts** — `MCP_CALL_TIMEOUT_S`, `PIPELINE_POLL_INTERVAL_S`
- **Network** — `LIGHTNING_BASE_PORT`, `LN_BIND_HOST`, `LN_ANNOUNCE_HOST`
- **UI Server** — `UI_HOST`, `UI_PORT`

---

## Status bar

The persistent top bar shows at a glance:

| Indicator | Source | Meaning |
|-----------|--------|---------|
| ⚡ indicator dot | `agent_lock` in `/api/status` | Green = agent running, red = no lock file |
| Agent lock | `runtime/agent/pipeline.lock` | `pid=NNNN` or "no lock" |
| Last request ID | Latest outbox entry | ID of the most recently completed pipeline run |
| Message count | Recent inbox length | How many messages are queued/visible |
| Build string | Latest outbox `pipeline_build` | Which pipeline version is running |
| **↺ Restart** | `/api/restart` | Runs `shutdown.sh && 1.start.sh` in a detached background process. The UI will briefly disconnect while the system restarts. Requires confirmation. |
| **⏻ Shutdown** | `/api/shutdown` | Runs `shutdown.sh` and stops all processes including this UI server. Requires confirmation. |

---

## API endpoints

The UI server exposes a lightweight REST + SSE API. All endpoints return JSON.

### GET endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Serves `web/index.html` |
| `GET /app.js` | Serves `web/app.js` |
| `GET /styles.css` | Serves `web/styles.css` |
| `GET /api/status` | Runtime snapshot: agent lock, last outbox, inbox count, last 10 inbox/outbox entries |
| `GET /api/network` | Lightning network topology: nodes and channels from `ln_listchannels` |
| `GET /api/logs` | List of archived pipeline run log files |
| `GET /api/logs/<filename>` | Content of a specific archive log file (JSON lines) |
| `GET /api/pipeline_result` | Most recent pipeline report from the outbox |
| `GET /api/stream` | Server-Sent Events stream — pushes `status`, `pipeline_result`, and `trace` events |
| `GET /api/tokens` | SSE stream of raw LLM token output (tails `runtime/agent/stream.jsonl`) |
| `GET /api/crash_kit` | Full debug snapshot: system info, config, runtime status, recent trace, metrics |
| `GET /api/config` | Current non-sensitive `.env` values for the settings form |

### POST endpoints

| Endpoint | Body | Description |
|----------|------|-------------|
| `POST /api/ask` | `{"prompt": "..."}` | Enqueues a natural language prompt for the AI pipeline |
| `POST /api/health` | `{}` | Enqueues a health check request |
| `POST /api/config` | `{"KEY": "value", ...}` | Writes config key-value pairs to `.env` |
| `POST /api/shutdown` | `{}` | Initiates system shutdown via `scripts/shutdown.sh` |
| `POST /api/restart` | `{}` | Restarts the full system via `shutdown.sh && 1.start.sh` |

---

## SSE event types

The `/api/stream` endpoint pushes three event types. The UI processes them using `EventSource`:

| Event type | Payload | When pushed |
|-----------|---------|------------|
| `status` | Runtime snapshot (same as `/api/status`) | Every ~2 seconds, and immediately on outbox file change |
| `pipeline_result` | Full `PipelineResult` dict | Whenever a new pipeline run completes |
| `trace` | List of recent trace events | Whenever new events are appended to `trace.log` |

---

## Running the UI server standalone

```bash
# Start the UI server independently (requires agent and infra already running)
source .venv/bin/activate
python -m scripts.ui_server

# Or specify a different port
UI_PORT=9000 python -m scripts.ui_server
```

In normal operation the UI server is started by `scripts/1.start.sh` as part of the full system launch.
