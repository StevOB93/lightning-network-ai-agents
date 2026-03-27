# API Reference

The web UI server (`scripts/ui_server.py`) exposes a REST + SSE API on `http://127.0.0.1:8008`. All responses are JSON unless noted otherwise.

When authentication is enabled (via `scripts/setup_security.sh`), all `/api/*` endpoints require a valid session cookie. Static files (`/`, `/*.html`, `/*.js`, `/*.css`) are served without auth so the login page loads.

## Authentication

### POST /api/login

Authenticate and receive a session cookie.

**Request:**
```json
{ "password": "your-admin-password" }
```

**Response (200):**
```json
{ "ok": true, "role": "admin", "csrf_token": "hex-string" }
```

Sets a `session` cookie (`HttpOnly; SameSite=Strict`). The `csrf_token` must be sent as `X-CSRF-Token` header on all subsequent POST requests.

**Response (401):** `{ "error": "Invalid password" }`

### POST /api/logout

Clear the session cookie.

**Response (200):** `{ "ok": true }`

## GET Endpoints

All GET endpoints return JSON. When auth is enabled, a valid session cookie is required.

### GET /api/status

Runtime snapshot for the status bar.

**Response:**
```json
{
  "agent_lock": "pid=1234 started_ts=1710871234",
  "last_outbox": { "request_id": 42, "pipeline_build": "pipeline-v1(...)" },
  "recent_inbox": [ ... ],
  "recent_outbox": [ ... ],
  "message_count": 5,
  "node_count": 2,
  "multi_agent": false,
  "agents": []
}
```

When `multi_agent` is true, `agents` contains per-agent status:
```json
{
  "agents": [
    { "node": 1, "online": true },
    { "node": 2, "online": false }
  ]
}
```

### GET /api/pipeline_result

The most recent pipeline result from the outbox.

**Response:**
```json
{
  "result": {
    "request_id": 42,
    "ts": 1710871234,
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
      { "tool": "ln_listfunds", "args": { "node": 1 }, "ok": true, "result": { ... } }
    ],
    "content": "Node 1 has 500,000 sat across 2 outputs.",
    "error": null,
    "pipeline_build": "pipeline-v1(translator+planner+executor+summarizer+history+verify)"
  }
}
```

`result` is `null` when no pipeline has run yet.

### GET /api/trace

Last 150 trace events from the live trace log.

**Response:**
```json
{
  "events": [
    { "ts": 1710871234, "event": "prompt_start", "req_id": 42, "user_text": "..." },
    { "ts": 1710871235, "event": "stage_timing", "req_id": 42, "translator_ms": 1200 },
    ...
  ]
}
```

### GET /api/network

Live network topology (calls MCP tools synchronously).

**Response:**
```json
{
  "nodes": [
    { "id": "02abc...", "nodeid": "02abc...", "alias": "node1", "running": true }
  ],
  "channels": [
    {
      "source": "02abc...",
      "destination": "03def...",
      "capacity": 1000000,
      "active": true
    }
  ]
}
```

### GET /api/config

Current configuration values for all allowed keys.

**Response:**
```json
{
  "LLM_BACKEND": "openai",
  "OPENAI_MODEL": "gpt-4o",
  "OPENAI_API_KEY": "sk-...x456",
  "OPENAI_API_KEY__set": true,
  ...
}
```

API keys are masked (first 3 + last 4 chars). The `KEY__set` boolean indicates whether a real key is configured.

### GET /api/logs

List archived query traces, sorted newest-first.

**Query parameters:**
- `q` — keyword filter (case-insensitive, max 200 chars)
- `status` — exact match: `ok`, `partial`, or `failed`

**Response:**
```json
[
  {
    "filename": "0042_20260320-143022_ok.jsonl",
    "req_id": 42,
    "datetime": "20260320-143022",
    "status": "ok",
    "size_bytes": 4096,
    "user_text_preview": "What is the balance of node 1?"
  }
]
```

### GET /api/logs/{filename}

Full events from a single archived trace file.

**Response:**
```json
{
  "filename": "0042_20260320-143022_ok.jsonl",
  "events": [ { "ts": ..., "event": "prompt_start", ... }, ... ]
}
```

**404** if the file doesn't exist. Path traversal attempts (`..", `/`, `\`) return 404.

### GET /api/metrics

Aggregate metrics over all archived queries.

**Response:**
```json
{
  "total_queries": 42,
  "status_counts": { "ok": 35, "partial": 3, "failed": 4 },
  "success_rate": 0.833,
  "stage_failure_counts": { "executor": 2, "translator": 1 },
  "avg_duration_s": 8.5
}
```

### GET /api/crash_kit

Debug snapshot for bug reports. Includes system info, config (non-sensitive), lock status, recent queue entries, metrics, and trace events.

## POST Endpoints

All POST endpoints require:
- Valid session cookie (when auth enabled)
- `X-CSRF-Token` header (when auth enabled)
- `Content-Type: application/json`
- Body size under 1 MB

### POST /api/ask

Submit a freeform prompt to the AI pipeline.

**Request:**
```json
{
  "text": "What is the balance of node 1?",
  "strategy": "conservative"
}
```

- `text` (required): prompt string, 1-10,000 chars
- `strategy` (optional): strategy profile name

**Response (200):**
```json
{ "queued": "ask", "msg": { "id": 43, "ts": 1710871234, "role": "user", "content": "...", "meta": { ... } } }
```

**Response (400):**
- `{ "error": "Missing 'text' prompt" }` — empty or missing text
- `{ "error": "Prompt too long (max 10,000 chars)" }` — text exceeds limit

### POST /api/health

Enqueue a health check ping.

**Response (200):**
```json
{ "queued": "health_check", "msg": { ... } }
```

### POST /api/config

Write configuration key-value pairs to `.env`.

**Request:**
```json
{
  "LLM_BACKEND": "gemini",
  "MCP_CALL_TIMEOUT_S": "60",
  "UNKNOWN_KEY": "ignored"
}
```

**Validation rules:**
- Only allowlisted keys are saved (unknown keys silently dropped)
- Values max 500 chars
- Control characters rejected
- Numeric fields (`MCP_CALL_TIMEOUT_S`, `UI_PORT`, etc.) must be positive integers
- Masked API key values (containing `...`) are silently dropped to prevent overwriting real keys

**Response (200):**
```json
{
  "saved": ["LLM_BACKEND", "MCP_CALL_TIMEOUT_S"],
  "rejected": ["SOME_INVALID_KEY"]
}
```

### POST /api/shutdown

Initiate system shutdown (runs `scripts/shutdown.sh`). Requires `system` permission (admin role).

**Response (200):** `{ "status": "shutdown_initiated" }`

### POST /api/restart

Full system restart (shutdown + start). Requires `system` permission.

**Response (200):** `{ "status": "restart_initiated" }`

### POST /api/restart_agent

Restart only the AI agent process. Requires `system` permission.

**Response (200):** `{ "status": "restart_initiated" }`

### POST /api/fresh

Fresh agent restart (archive + clear inbox/outbox, then restart). Requires `system` permission.

**Response (200):** `{ "status": "fresh_restart_initiated" }`

## SSE Endpoints

### GET /api/stream

Server-Sent Events stream for live updates. Polls file mtimes every 400ms.

**Event types:**

| Event | Trigger | Data |
|-------|---------|------|
| `status` | Inbox or outbox file changes | Same as `GET /api/status` |
| `pipeline_result` | Outbox file changes | `{ "result": PipelineResult }` |
| `trace` | Trace log file changes | `{ "events": [...] }` (last 50) |

An initial snapshot of all three event types is sent immediately on connect.

### GET /api/tokens

Server-Sent Events stream for LLM summary token streaming. Polls every 50ms.

**Event type:** Always `token`.

**Data:**
```json
{ "event": "stream_start", "req_id": 42, "ts": 1710871234 }
{ "event": "token", "text": "Node 1 has " }
{ "event": "token", "text": "500,000 sat" }
{ "event": "stream_end", "req_id": 42, "ts": 1710871236 }
```

## RBAC Permissions

When auth is enabled, endpoints require specific permissions:

| Permission | Endpoints | Roles |
|-----------|-----------|-------|
| `read` | All GET endpoints | admin, viewer |
| `write` | `/api/ask`, `/api/health` | admin |
| `config` | `POST /api/config` | admin |
| `system` | `/api/shutdown`, `/api/restart`, `/api/restart_agent`, `/api/fresh` | admin |

## Error Responses

| Status | Meaning |
|--------|---------|
| 400 | Bad request (missing field, invalid value, payload too large) |
| 401 | Authentication required (no/invalid session cookie) |
| 403 | Forbidden (insufficient RBAC permissions or invalid CSRF token) |
| 404 | Not found (unknown endpoint or missing archive file) |
| 429 | Rate limited (includes `Retry-After` header) |

## Rate Limits

Per-IP sliding window (60-second window):
- **Global**: 100 requests/minute
- **Sensitive** (`/api/login`, `/api/config`, `/api/ask`): 10 requests/minute
- **Login**: 5 attempts/minute
