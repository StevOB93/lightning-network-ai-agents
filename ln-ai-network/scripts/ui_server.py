"""Lightning Network AI Agent — Web UI Server (v2)

Serves static files from web/ and exposes API endpoints that expose the full
3-stage pipeline state: translator intent, planner steps, executor results,
live trace log, network topology, and the trace archive.

Architecture notes:
  - Uses Python's built-in ThreadingHTTPServer so each request gets its own
    thread. This allows the SSE /api/stream endpoint to block in its polling
    loop without preventing other requests from being served.
  - All state is read from files on disk (inbox.jsonl, outbox.jsonl, trace.log,
    runtime/agent/logs/). There is no in-process state shared between requests,
    which makes the server crash-safe and restart-safe.
  - The SSE endpoint polls file modification times (mtime) rather than the
    content, so it only reads the file when something actually changed.
"""
from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

# Resolve repo root relative to this script's location
REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = REPO_ROOT / "web"
RUNTIME_DIR = REPO_ROOT / "runtime" / "agent"
ENV_FILE = REPO_ROOT / ".env"

# Config keys that may be read/written via /api/config.
_PIPELINE_ROLES = ("TRANSLATOR", "PLANNER", "EXECUTOR", "SUMMARIZER")
_CONFIG_KEYS: frozenset[str] = frozenset({
    "LLM_BACKEND",
    "OPENAI_MODEL",
    "OLLAMA_MODEL",
    "GEMINI_MODEL",
    "CLAUDE_MODEL",
    "OLLAMA_BASE_URL",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "ANTHROPIC_API_KEY",
    "MCP_CALL_TIMEOUT_S",
    "MCP_NODE_START_TIMEOUT_S",
    "MCP_NODE_STOP_TIMEOUT_S",
    "LN_BIND_HOST",
    "LN_ANNOUNCE_HOST",
    "REGTEST_TARGET_HEIGHT",
    "UI_HOST",
    "UI_PORT",
    # Per-stage LLM backend overrides (e.g. TRANSLATOR_LLM_BACKEND=gemini)
    *(f"{r}_LLM_BACKEND" for r in _PIPELINE_ROLES),
    # Per-stage model overrides (e.g. TRANSLATOR_OPENAI_MODEL=gpt-4o-mini)
    *(f"{r}_{b}_MODEL" for r in _PIPELINE_ROLES
      for b in ("OPENAI", "OLLAMA", "GEMINI", "CLAUDE")),
})

# API key env vars — returned masked (never exposed in full via the HTTP API).
_API_KEY_KEYS: frozenset[str] = frozenset({
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "ANTHROPIC_API_KEY",
})

_API_KEY_PLACEHOLDERS = {
    "__REPLACE_WITH_REAL_KEY__",
    "__PASTE_YOUR_OPENAI_KEY_HERE__",
    "__PASTE_YOUR_GEMINI_KEY_HERE__",
    "__PASTE_YOUR_ANTHROPIC_KEY_HERE__",
}


def _mask_api_key(value: str) -> str:
    """Return a masked version of an API key for safe display."""
    if not value or value in _API_KEY_PLACEHOLDERS:
        return ""
    if len(value) < 8:
        return "****"
    return value[:3] + "..." + value[-4:]


def _is_masked_value(value: str) -> bool:
    """True if value looks like a masked placeholder rather than a real key."""
    return not value or value == "****" or "..." in value

# Ensure the repo root is on sys.path so we can import ai.* and mcp.*
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ai.command_queue import enqueue, last_outbox, paths
from mcp.ln_mcp_server import handle as mcp_handle

# Security utilities — auth, CSRF, rate limiting, audit
from scripts.security import (
    HTTPRateLimiter,
    SecurityAuditLogger,
    check_permission,
    create_session_token,
    generate_csrf_token,
    validate_csrf_token,
    validate_session_token,
    verify_password,
)

# ---------------------------------------------------------------------------
# Security configuration
# ---------------------------------------------------------------------------

# When True, all API endpoints require a valid session cookie.
# Disabled when UI_ADMIN_PASSWORD_HASH is not set (backward compatible).
_AUTH_ENABLED = bool(os.getenv("UI_ADMIN_PASSWORD_HASH", ""))

_SESSION_SECRET = os.getenv("UI_SESSION_SECRET", "")
_SESSION_TTL = int(os.getenv("UI_SESSION_TTL_S", "3600"))
_ADMIN_HASH = os.getenv("UI_ADMIN_PASSWORD_HASH", "")
_VIEWER_HASH = os.getenv("UI_VIEWER_PASSWORD_HASH", "")
_CORS_ORIGIN = os.getenv("UI_CORS_ORIGIN", "")
_TLS_ENABLED = bool(os.getenv("UI_TLS_CERT", "") and os.getenv("UI_TLS_KEY", ""))

# Paths that do NOT require authentication (static assets + login endpoint).
_AUTH_EXEMPT_PATHS: frozenset[str] = frozenset({
    "/api/login",
})

# Static file extensions served without auth (the login page needs these).
_STATIC_EXTENSIONS = (".html", ".css", ".js", ".svg", ".ico")

# Shared rate limiter and audit logger (created once at module level).
_rate_limiter = HTTPRateLimiter()
_audit_logger = SecurityAuditLogger(
    log_path=RUNTIME_DIR / "security_audit.jsonl" if RUNTIME_DIR else None,
)

# ---------------------------------------------------------------------------
# Named constants — keep magic numbers in one place for easy tuning
# ---------------------------------------------------------------------------
JSONL_TAIL_LIMIT       = 20       # Default max lines for _read_jsonl_tail
TRACE_TAIL_LIMIT_REST  = 150      # Trace events returned by /api/trace (REST poll)
TRACE_TAIL_LIMIT_SSE   = 50       # Trace events per SSE push (compact payloads)
ARCHIVE_READ_LIMIT     = 10_000   # Max lines when reading an archived trace file
QUEUE_DISPLAY_LIMIT    = 10       # Inbox/outbox entries in /api/status snapshot
USER_TEXT_PREVIEW_CHARS = 120     # Truncated preview in archive list view
SSE_POLL_INTERVAL_S    = 0.4      # File mtime poll interval for SSE stream (seconds)
TOKEN_POLL_INTERVAL_S  = 0.05     # Token streaming poll interval (seconds)


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def _read_jsonl_tail(path: Path, limit: int = JSONL_TAIL_LIMIT) -> list[dict[str, Any]]:
    """
    Read the last `limit` valid JSON objects from a JSONL file.

    Reads the entire file into memory and takes a tail slice. This is efficient
    enough for the small files used here (trace.log, inbox, outbox), but would
    need a seek-from-end approach for very large files.

    Malformed lines (truncated writes, partial flushes) are silently skipped
    so a mid-write read never causes an error response to the browser.
    """
    if not path.exists():
        return []
    lines = [ln for ln in path.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()]
    out: list[dict[str, Any]] = []
    for ln in lines[-limit:]:
        try:
            obj = json.loads(ln)
        except Exception:
            continue  # Skip malformed lines (e.g. truncated by mid-write read)
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _read_trace_tail(limit: int = TRACE_TAIL_LIMIT_REST) -> list[dict[str, Any]]:
    """
    Return the last `limit` events from the live trace.log.

    Used by both /api/trace (REST poll) and /api/stream (SSE push).
    The SSE stream uses a smaller limit (50) to keep individual push payloads
    compact; the REST endpoint uses 150 so a late-joining client gets more history.
    """
    return _read_jsonl_tail(RUNTIME_DIR / "trace.log", limit=limit)


def _list_archives(q: str = "", status: str = "") -> list[dict[str, Any]]:
    """
    Return metadata for all archived query traces, sorted newest-first.

    Each archive file is named: {req_id:04d}_{YYYYMMDD-HHMMSS}_{status}.jsonl
    The filename is parsed to extract req_id, datetime, and status without
    reading the full file. Only the first line (the prompt_start header) is
    read to extract the user_text_preview.

    Files that don't match the three-part naming convention are silently skipped
    (they might be temp files or manually placed files).

    Optional filters:
      q      — case-insensitive keyword match against user_text_preview
      status — exact match against the status field ("ok", "partial", "failed")
    """
    logs_dir = RUNTIME_DIR / "logs"
    if not logs_dir.exists():
        return []
    results = []
    # Reverse sort: since filenames start with zero-padded req_id,
    # lexicographic sort == chronological sort, and reverse gives newest-first.
    for p in sorted(logs_dir.glob("*.jsonl"), reverse=True):
        parts = p.stem.split("_", 2)  # Split at most twice: req_id, datetime, status
        if len(parts) != 3:
            continue  # Skip non-conforming filenames
        req_id_str, dt_str, file_status = parts
        # Apply status filter before reading file contents (fast path)
        if status and file_status != status:
            continue
        user_text = ""
        try:
            # Read only the first line (the prompt_start header) to get user_text.
            # Using a with-block ensures the file handle is always closed.
            with p.open(encoding="utf-8", errors="ignore") as fh:
                first_line = fh.readline()
            user_text = json.loads(first_line).get("user_text", "")
        except Exception:
            pass  # Missing or malformed header — preview stays empty
        # Apply keyword filter after reading user_text (case-insensitive)
        if q and q.lower() not in user_text.lower():
            continue
        results.append({
            "filename": p.name,
            "req_id": int(req_id_str) if req_id_str.isdigit() else 0,
            "datetime": dt_str,               # Raw string e.g. "20260319-143022"
            "status": file_status,            # "ok" | "failed" | "partial"
            "size_bytes": p.stat().st_size,
            "user_text_preview": user_text[:USER_TEXT_PREVIEW_CHARS],
        })
    return results


def _compute_metrics() -> dict[str, Any]:
    """
    Compute aggregate metrics over all archived query traces.

    Reads every JSONL file in runtime/agent/logs/ and aggregates:
      total_queries       — count of conforming archive files
      status_counts       — {"ok": N, "partial": N, "failed": N}
      success_rate        — float 0-1 (ok / total_queries, 0 if no queries)
      stage_failure_counts— dict of stage names → count of failures
                            parsed from "stage_failed" events inside each file
      avg_duration_s      — average query duration in seconds (first ts to last ts);
                            None if no files have timestamp data

    Reading every archive file on each request is fine because the logs
    directory is expected to stay small (one file per completed query).
    """
    logs_dir = RUNTIME_DIR / "logs"
    if not logs_dir.exists():
        return {
            "total_queries": 0,
            "status_counts": {"ok": 0, "partial": 0, "failed": 0},
            "success_rate": 0.0,
            "stage_failure_counts": {},
            "avg_duration_s": None,
        }

    status_counts: dict[str, int] = {"ok": 0, "partial": 0, "failed": 0}
    stage_failure_counts: dict[str, int] = {}
    durations: list[float] = []
    stage_timing_ms: dict[str, list[float]] = {
        "translator": [], "planner": [], "executor": [], "summarizer": []
    }
    total = 0

    for p in logs_dir.glob("*.jsonl"):
        parts = p.stem.split("_", 2)
        if len(parts) != 3:
            continue  # Skip non-conforming filenames
        _, _, file_status = parts
        total += 1
        # Tally status — unknown statuses go into whichever bucket matches, or ignored
        if file_status in status_counts:
            status_counts[file_status] += 1

        # Read the file to gather stage_failed events, timestamps, and stage timing
        timestamps: list[float] = []
        try:
            lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if not isinstance(ev, dict):
                    continue
                # Collect timestamps for duration calculation
                ts = ev.get("ts")
                if isinstance(ts, (int, float)) and ts > 0:
                    timestamps.append(float(ts))
                # Count stage failures — look for stage_failed field
                stage = ev.get("stage_failed")
                if stage and isinstance(stage, str):
                    stage_failure_counts[stage] = stage_failure_counts.get(stage, 0) + 1
                # Collect per-stage timing from stage_timing events
                if ev.get("event") == "stage_timing":
                    for key in ("translator", "planner", "executor", "summarizer"):
                        val = ev.get(f"{key}_ms")
                        if isinstance(val, (int, float)) and val >= 0:
                            stage_timing_ms[key].append(float(val))
        except Exception:
            pass  # Unreadable file — skip it

        if len(timestamps) >= 2:
            durations.append(max(timestamps) - min(timestamps))

    success_rate = (status_counts.get("ok", 0) / total) if total > 0 else 0.0
    avg_duration_s = (sum(durations) / len(durations)) if durations else None

    # Compute per-stage average timing (None if no data for that stage)
    avg_stage_ms = {
        stage: round(sum(vals) / len(vals), 1) if vals else None
        for stage, vals in stage_timing_ms.items()
    }

    return {
        "total_queries": total,
        "status_counts": status_counts,
        "success_rate": success_rate,
        "stage_failure_counts": stage_failure_counts,
        "avg_duration_s": avg_duration_s,
        "avg_stage_ms": avg_stage_ms,
    }


def _read_archive(filename: str) -> dict[str, Any] | None:
    """
    Return all events from a single archived trace file.

    Security: rejects any filename containing path separators or ".." to prevent
    directory traversal attacks. The filename is then joined to the fixed
    logs_dir path so even a bypassed check couldn't escape the directory.

    Returns None (→ 404) if the file is missing or the filename is invalid.
    """
    # Reject path components that could escape the logs directory.
    # Decode URL-encoded characters first so %2f etc. can't bypass the check.
    from urllib.parse import unquote
    filename = unquote(filename)
    if "/" in filename or "\\" in filename or ".." in filename:
        return None
    p = RUNTIME_DIR / "logs" / filename
    if not p.exists() or not p.is_file():
        return None
    # limit=10000 effectively reads the whole file — archive files are bounded
    # by the duration of a single query (typically a few hundred events).
    return {"filename": filename, "events": _read_jsonl_tail(p, limit=ARCHIVE_READ_LIMIT)}


# ---------------------------------------------------------------------------
# Pipeline result extraction
# ---------------------------------------------------------------------------

def _latest_pipeline_result() -> dict[str, Any] | None:
    """
    Return the most recent outbox entry that looks like a pipeline report.

    Scans the last 30 outbox entries in reverse (newest first) looking for
    an entry with type="pipeline_report" or the presence of "step_results"
    or "intent" fields (legacy format). Falls back to last_outbox() which
    returns the single most recent entry regardless of type.
    """
    qp = paths()
    outbox = _read_jsonl_tail(qp.outbox, limit=30)
    for entry in reversed(outbox):
        if entry.get("type") == "pipeline_report" or "step_results" in entry or "intent" in entry:
            return entry
    return last_outbox()


# ---------------------------------------------------------------------------
# Network topology extraction
# ---------------------------------------------------------------------------

def _unwrap_payload(raw: Any) -> dict:
    """
    Try to unwrap nested tool result payloads into a flat dict.

    MCP tool results use a nested shape: {ok: bool, result: {ok: bool, payload: {...}}}.
    This helper tries common nesting patterns to extract the inner data dict.
    Falls back to the original dict if no known nesting is found.
    """
    if not isinstance(raw, dict):
        try:
            raw = json.loads(raw) if isinstance(raw, str) else {}
        except Exception:
            return {}
    # Try common nesting keys in order of specificity
    for key in ("result", "payload", "data"):
        if key in raw and isinstance(raw[key], dict):
            inner = raw[key]
            # One more level: result.payload (the most common real shape)
            if "payload" in inner and isinstance(inner["payload"], dict):
                return inner["payload"]
            return inner
    return raw


def _extract_network_data() -> dict[str, Any]:
    """
    Fetch live network topology by calling MCP tools directly (not via the pipeline).

    This runs synchronously in the HTTP request handler thread. It calls:
      1. network_health → list of nodes with running status
      2. ln_getinfo(node=N) for each running node → pubkey + alias
      3. ln_listchannels(node=N) for the first running node → channel list

    The result is a {nodes, channels} structure consumed by the D3 force graph
    in the frontend. We only call ln_listchannels on ONE node because channels
    are bidirectional — listing from both ends would create duplicate edges.

    Returns {"nodes": [], "channels": []} on any top-level error.
    """
    try:
        raw = mcp_handle("network_health", {})
    except Exception:
        import traceback
        traceback.print_exc()
        return {"nodes": [], "channels": []}

    nodes: list[dict] = []
    node_pubkeys: dict[str, str] = {}  # Maps node_name → pubkey for channel source lookup

    for n in raw.get("nodes", []):
        status = n.get("status", {})
        payload = status.get("payload", {}) if isinstance(status, dict) else {}
        node_name = n.get("name", "unknown")
        running = payload.get("running", False) if isinstance(payload, dict) else False
        node_obj: dict[str, Any] = {
            "id": node_name,         # Default to name; overwritten with pubkey if available
            "nodeid": node_name,
            "alias": node_name,
            "running": running,
        }

        # For running nodes, fetch the real pubkey and alias from ln_getinfo.
        # The pubkey is needed to match channel source/destination endpoints.
        if running:
            try:
                info = mcp_handle("ln_getinfo", {"node": node_name})
                p = info.get("payload", {}) if info.get("ok") else {}
                if isinstance(p, dict) and p.get("id"):
                    node_obj["id"] = p["id"]
                    node_obj["nodeid"] = p["id"]
                    node_obj["alias"] = p.get("alias", node_name)
                    node_pubkeys[node_name] = p["id"]
            except Exception:
                pass  # Missing getinfo → use the name-based fallback
        nodes.append(node_obj)

    # Get channels from the first running node only (channels are bidirectional,
    # so one node's view gives us all edges in the graph).
    channels: list[dict] = []
    for n in raw.get("nodes", []):
        node_name = n.get("name", "unknown")
        status = n.get("status", {})
        payload = status.get("payload", {}) if isinstance(status, dict) else {}
        running = payload.get("running", False) if isinstance(payload, dict) else False
        if not running:
            continue
        try:
            ch_result = mcp_handle("ln_listchannels", {"node": node_name})
            if ch_result.get("ok"):
                ch_payload = ch_result.get("payload", {})
                for ch in ch_payload.get("channels", []):
                    channels.append({
                        # source may be the pubkey or the node name depending on CLN version
                        "source": ch.get("source", node_pubkeys.get(node_name, node_name)),
                        "destination": ch.get("destination", ch.get("peer_id", "")),
                        "capacity": ch.get("satoshis", ch.get("capacity", 0)),
                        # CHANNELD_NORMAL is the CLN state for a fully operational channel
                        "active": ch.get("active", ch.get("state") == "CHANNELD_NORMAL"),
                    })
        except Exception:
            pass
        break  # Only query one node (see docstring above)

    return {"nodes": nodes, "channels": channels}


# ---------------------------------------------------------------------------
# Runtime snapshot
# ---------------------------------------------------------------------------

def _read_node_count() -> int:
    """Read the node count written by 1.start.sh to runtime/node_count."""
    try:
        return int((REPO_ROOT / "runtime" / "node_count").read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def _runtime_snapshot() -> dict[str, Any]:
    """
    Build a lightweight status snapshot for the status bar and queue panels.

    Reads the agent/pipeline lock file to determine if the backend process is
    running, and the last 10 inbox/outbox entries for the queue panels.

    Checks for pipeline.lock first (pipeline mode), then agent.lock (legacy mode).
    """
    qp = paths()
    inbox = _read_jsonl_tail(qp.inbox, limit=QUEUE_DISPLAY_LIMIT)
    outbox = _read_jsonl_tail(qp.outbox, limit=QUEUE_DISPLAY_LIMIT)

    # Try pipeline.lock first, then legacy agent.lock
    lock_text = ""
    lock_path = qp.base_dir / "pipeline.lock"
    if not lock_path.exists():
        lock_path = qp.base_dir / "agent.lock"
    if lock_path.exists():
        lock_text = lock_path.read_text(encoding="utf-8", errors="ignore").strip()

    return {
        "agent_lock": lock_text,        # "pid=1234 started_ts=1710871234" or ""
        "last_outbox": last_outbox(),   # Most recent outbox entry (any type)
        "recent_inbox": inbox,          # Last 10 inbox entries
        "recent_outbox": outbox,        # Last 10 outbox entries
        "message_count": len(inbox),    # Count of recent inbox entries shown
    }


# ---------------------------------------------------------------------------
# Crash kit
# ---------------------------------------------------------------------------

def _crash_kit() -> dict[str, Any]:
    """
    Build a comprehensive debug snapshot for bug reports.

    Collects everything a developer would need to diagnose a runtime problem:
    system info, node count, current config (non-sensitive), runtime lock status,
    recent queue entries, last pipeline result, recent trace events, and metrics.

    All data is gathered in a single call so the snapshot is consistent in time.
    """
    import platform
    return {
        "generated_at": time.time(),
        "system": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "node_count": _read_node_count(),
        },
        "runtime": _runtime_snapshot(),
        "pipeline_result": _latest_pipeline_result(),
        "trace": _read_trace_tail(limit=100),
        "metrics": _compute_metrics(),
        "config": _read_config(),
    }


# ---------------------------------------------------------------------------
# Config read / write
# ---------------------------------------------------------------------------

def _read_env_file() -> dict[str, str]:
    """
    Parse .env as KEY=VALUE lines, skipping comments and blank lines.
    Returns an empty dict if the file doesn't exist.
    """
    result: dict[str, str] = {}
    if not ENV_FILE.exists():
        return result
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            result[key.strip()] = val.strip()
    return result


def _read_config() -> dict[str, Any]:
    """
    Return current values for all allowed config keys.

    Priority: live env var (set at process start) → .env file value → empty string.
    Both sources are returned so the UI can distinguish "saved in .env" from
    "only in current environment (not persisted)".

    API keys are returned masked (first 3 + last 4 chars only) with a companion
    ``KEY__set`` boolean so the UI can show a status badge without ever receiving
    the full secret.
    """
    env_file = _read_env_file()
    result: dict[str, Any] = {}
    for key in _CONFIG_KEYS:
        result[key] = os.getenv(key, env_file.get(key, ""))

    # Mask API keys — never expose the full secret via HTTP
    for key in _API_KEY_KEYS:
        raw = result.get(key, "")
        is_set = bool(raw) and raw not in _API_KEY_PLACEHOLDERS
        result[key + "__set"] = is_set
        result[key] = _mask_api_key(raw)

    return result


def _write_config(updates: dict[str, str]) -> None:
    """
    Merge updates into .env, preserving existing lines and comments.

    Only keys in _CONFIG_KEYS are accepted; unknown keys are silently dropped.
    API keys that look masked (empty, "****", or contain "...") are also dropped
    so that a load→save round-trip never overwrites a real key with its mask.
    Existing lines for a key are updated in-place; new keys are appended at the end.
    """
    filtered = {k: v for k, v in updates.items() if k in _CONFIG_KEYS}
    # Never overwrite a real API key with a masked placeholder
    for key in _API_KEY_KEYS:
        if key in filtered and _is_masked_value(filtered[key]):
            del filtered[key]
    if not filtered:
        return

    existing_lines: list[str] = []
    if ENV_FILE.exists():
        existing_lines = ENV_FILE.read_text(encoding="utf-8").splitlines()

    updated_keys: set[str] = set()
    new_lines: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            new_lines.append(line)
            continue
        if "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in filtered:
                new_lines.append(f"{key}={filtered[key]}")
                updated_keys.add(key)
                continue
        new_lines.append(line)

    # Append keys not already present in the file
    for key, val in filtered.items():
        if key not in updated_keys:
            new_lines.append(f"{key}={val}")

    ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class UIHandler(BaseHTTPRequestHandler):
    """
    HTTP request handler for the web UI.

    GET endpoints:
      /api/status         → runtime snapshot (lock status, queue counts)
      /api/pipeline_result → latest pipeline report from outbox
      /api/trace          → last 150 live trace events
      /api/network        → live network topology (calls MCP tools)
      /api/logs           → archive list metadata (newest-first)
      /api/logs/{name}    → full events for one archive file
      /api/config         → current values for all allowed config keys
      /api/stream         → SSE stream of live updates
      /api/tokens         → SSE stream of LLM summary tokens (near-real-time)
      /*                  → static files from web/

    POST endpoints:
      /api/ask            → enqueue a freeform prompt for the pipeline
      /api/health         → enqueue a health check ping
      /api/config         → write/merge key-value pairs into .env file
      /api/shutdown       → run scripts/shutdown.sh (stops all processes)
      /api/restart        → run shutdown.sh then 1.start.sh (full system restart)
    """

    server_version = "LightningAgentUI/2.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # Suppress the default per-request access log to keep stdout clean

    # ------------------------------------------------------------------
    # Security helpers
    # ------------------------------------------------------------------

    def _send_security_headers(self) -> None:
        """Send standard security headers on every response."""
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' https://d3js.org; "
            "style-src 'self' https://fonts.googleapis.com 'unsafe-inline'; "
            "font-src https://fonts.gstatic.com; "
            "connect-src 'self'; "
            "img-src 'self' data:; "
            "frame-ancestors 'none'",
        )
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if _TLS_ENABLED:
            self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")

    def _send_cors_headers(self) -> None:
        """Send CORS headers (only when a specific origin is configured)."""
        if _CORS_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", _CORS_ORIGIN)
            self.send_header("Vary", "Origin")

    def _get_client_ip(self) -> str:
        """Return client IP, respecting X-Forwarded-For behind a reverse proxy."""
        forwarded = self.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return self.client_address[0]

    def _get_session_cookie(self) -> str:
        """Extract the session token from the Cookie header."""
        cookie_header = self.headers.get("Cookie", "")
        if not cookie_header:
            return ""
        cookie: SimpleCookie[str] = SimpleCookie()
        try:
            cookie.load(cookie_header)
        except Exception:
            return ""
        morsel = cookie.get("session")
        return morsel.value if morsel else ""

    def _check_auth(self) -> dict[str, str] | None:
        """Validate the session cookie.

        Returns ``{"user_id": ..., "role": ...}`` on success, ``None`` on
        failure.  When auth is disabled (no password hash configured) returns
        a synthetic admin identity so all code paths work uniformly.
        """
        if not _AUTH_ENABLED:
            return {"user_id": "admin", "role": "admin"}
        token = self._get_session_cookie()
        if not token or not _SESSION_SECRET:
            return None
        return validate_session_token(token, _SESSION_SECRET)

    def _check_rate_limit(self) -> bool:
        """Check per-IP rate limit. Returns True if the request should be rejected."""
        ip = self._get_client_ip()
        parsed = urlparse(self.path)
        retry_after = _rate_limiter.check(ip, self.command, parsed.path)
        if retry_after is not None:
            self.send_response(HTTPStatus.TOO_MANY_REQUESTS)
            self.send_header("Retry-After", str(retry_after))
            self._send_security_headers()
            self.end_headers()
            return True
        return False

    def _check_csrf(self, session_token: str) -> bool:
        """Validate the X-CSRF-Token header for POST requests.

        Returns True if CSRF validation passes (or is not required).
        Returns False if the token is missing/invalid — caller should abort.
        """
        if not _AUTH_ENABLED:
            return True
        csrf_header = self.headers.get("X-CSRF-Token", "")
        if not csrf_header:
            return False
        return validate_csrf_token(csrf_header, session_token, _SESSION_SECRET)

    # ------------------------------------------------------------------
    # Response helpers
    # ------------------------------------------------------------------

    def _json(self, status: int, payload: Any) -> None:
        """
        Send a JSON response with security headers.

        default=str ensures non-serializable types (Path, datetime, etc.) are
        converted to strings rather than raising TypeError.
        """
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._send_cors_headers()
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, rel_path: str) -> None:
        """
        Serve a static file from the web/ directory.

        Security: resolves the canonical path and checks that it's inside
        WEB_ROOT before serving. This prevents path traversal attacks like
        /api/../../../etc/passwd.

        Defaults to index.html for / or empty path (single-page app behaviour).
        """
        rel = "index.html" if rel_path in ("", "/") else rel_path.lstrip("/")
        file_path = (WEB_ROOT / rel).resolve()
        web_root_resolved = WEB_ROOT.resolve()
        # Strict containment check: the resolved path must be inside WEB_ROOT
        if web_root_resolved not in file_path.parents and file_path != web_root_resolved:
            self._json(HTTPStatus.FORBIDDEN, {"error": "Forbidden"})
            return
        if not file_path.exists() or not file_path.is_file():
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        ext_map = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
        }
        ctype = ext_map.get(file_path.suffix, "application/octet-stream")
        data = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._send_cors_headers()
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self) -> None:  # noqa: N802
        """
        Handle CORS preflight requests from the browser.
        Required for cross-origin POST requests (e.g. from a dev server).
        """
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-CSRF-Token")
        self._send_security_headers()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self._check_rate_limit():
            return

        parsed = urlparse(self.path)
        route = parsed.path

        # Static files are always served (login page needs them).
        if not route.startswith("/api/"):
            self._serve_static(parsed.path)
            return

        # Auth gate — all /api/ endpoints require a valid session (when enabled).
        identity = self._check_auth()
        if identity is None:
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "Authentication required"})
            return

        # RBAC — check if the user's role has permission for this endpoint.
        if not check_permission(identity["role"], "GET", route):
            self._json(HTTPStatus.FORBIDDEN, {"error": "Insufficient permissions"})
            return

        if route == "/api/status":
            self._json(HTTPStatus.OK, _runtime_snapshot())
        elif route == "/api/pipeline_result":
            self._json(HTTPStatus.OK, {"result": _latest_pipeline_result()})
        elif route == "/api/trace":
            self._json(HTTPStatus.OK, {"events": _read_trace_tail()})
        elif route == "/api/network":
            self._json(HTTPStatus.OK, _extract_network_data())
        elif route == "/api/logs":
            qs = parse_qs(parsed.query)
            q_param = qs.get("q", [""])[0][:200]  # Max 200 chars
            status_param = qs.get("status", [""])[0]
            if status_param and status_param not in ("ok", "partial", "failed"):
                status_param = ""
            self._json(HTTPStatus.OK, _list_archives(q=q_param, status=status_param))
        elif route.startswith("/api/logs/"):
            filename = route[len("/api/logs/"):]
            result = _read_archive(filename)
            if result is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Archive not found"})
            else:
                self._json(HTTPStatus.OK, result)
        elif route == "/api/metrics":
            self._json(HTTPStatus.OK, _compute_metrics())
        elif route == "/api/crash_kit":
            self._json(HTTPStatus.OK, _crash_kit())
        elif route == "/api/config":
            self._json(HTTPStatus.OK, _read_config())
        elif route == "/api/stream":
            self._sse_stream()
        elif route == "/api/tokens":
            self._sse_tokens()
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Unknown endpoint"})

    def _sse_stream(self) -> None:
        """
        Server-Sent Events endpoint — pushes live updates to the browser.

        Protocol:
          - The client opens a long-lived GET /api/stream connection.
          - The server sends named events in the format:
              event: <name>\n
              data: <json>\n
              \n
          - Three event types are pushed:
              "status"          → runtime snapshot (lock, queue counts)
              "pipeline_result" → latest pipeline report
              "trace"           → last 50 live trace events

        Polling strategy: instead of watching inotify events (which would
        require an extra dependency), we poll file mtimes every 400ms.
        This keeps the implementation simple and cross-platform while
        delivering updates to the browser within ~400ms of each write.

        X-Accel-Buffering: no prevents nginx (if used as a reverse proxy)
        from buffering the SSE stream, which would delay event delivery.

        Connection handling: BrokenPipeError/ConnectionResetError mean the
        client disconnected. We catch these and return cleanly rather than
        logging a stack trace for every browser tab close.
        """
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self._send_cors_headers()
        self.send_header("X-Accel-Buffering", "no")  # Disable nginx proxy buffering
        self.end_headers()

        def send(event: str, data: Any) -> bool:
            """
            Write a single SSE event to the response stream.
            Returns False if the client has disconnected, True on success.
            """
            try:
                msg = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
                self.wfile.write(msg.encode("utf-8"))
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False  # Client disconnected

        qp = paths()
        # Track last-seen mtimes so we only push when files actually change.
        # Starting at 0.0 ensures the initial snapshot is always sent.
        last_outbox_mtime: float = 0.0
        last_trace_mtime: float = 0.0
        last_inbox_mtime: float = 0.0

        # Send a full state snapshot immediately on connect so the browser
        # doesn't show a blank UI while waiting for the first poll cycle.
        if not send("status", _runtime_snapshot()):
            return
        result = _latest_pipeline_result()
        if result:
            if not send("pipeline_result", {"result": result}):
                return
        # Send a smaller trace slice on connect (compact vs REST's full history)
        if not send("trace", {"events": _read_trace_tail(limit=TRACE_TAIL_LIMIT_SSE)}):
            return

        try:
            while True:
                time.sleep(SSE_POLL_INTERVAL_S)

                # Outbox changed → a pipeline run completed; push result + status
                if qp.outbox.exists():
                    mtime = qp.outbox.stat().st_mtime
                    if mtime > last_outbox_mtime:
                        last_outbox_mtime = mtime
                        if not send("status", _runtime_snapshot()):
                            return
                        r = _latest_pipeline_result()
                        if r and not send("pipeline_result", {"result": r}):
                            return

                # Trace log changed → a pipeline stage emitted new events; push them
                trace_path = RUNTIME_DIR / "trace.log"
                if trace_path.exists():
                    mtime = trace_path.stat().st_mtime
                    if mtime > last_trace_mtime:
                        last_trace_mtime = mtime
                        if not send("trace", {"events": _read_trace_tail(limit=50)}):
                            return

                # Inbox changed → a new command was enqueued; update status bar counts
                if qp.inbox.exists():
                    mtime = qp.inbox.stat().st_mtime
                    if mtime > last_inbox_mtime:
                        last_inbox_mtime = mtime
                        if not send("status", _runtime_snapshot()):
                            return

        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # Client disconnected cleanly — no error logging needed

    def _sse_tokens(self) -> None:
        """
        Server-Sent Events endpoint for streaming LLM summary tokens.

        Tails runtime/agent/stream.jsonl starting from the current end-of-file
        so only tokens generated after the client connects are delivered. Polls
        at 50ms intervals for near-real-time delivery.

        Event type: always "token". Data is the raw JSON object from stream.jsonl:
          {"event": "stream_start", "req_id": N, "ts": ...}  — new query started
          {"event": "token",        "text":  "..."}           — one LLM token chunk
          {"event": "stream_end",   "req_id": N, "ts": ...}  — summarizer done

        The browser uses stream_start/stream_end to show/hide a typing cursor
        and to know when the streaming preview is complete.
        """
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self._send_cors_headers()
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def send(data: Any) -> bool:
            try:
                msg = f"event: token\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                self.wfile.write(msg.encode("utf-8"))
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False

        stream_path = RUNTIME_DIR / "stream.jsonl"
        # Seek to end of current file — only deliver tokens written after connect.
        # This prevents replaying old query tokens to newly-connected clients.
        pos = stream_path.stat().st_size if stream_path.exists() else 0

        try:
            while True:
                time.sleep(TOKEN_POLL_INTERVAL_S)
                if not stream_path.exists():
                    pos = 0  # file removed — reset so we catch it when recreated
                    continue
                try:
                    cur_size = stream_path.stat().st_size
                except Exception:
                    continue
                if cur_size < pos:
                    # File was truncated/recreated — reset to start of new content
                    pos = 0
                try:
                    with stream_path.open("r", encoding="utf-8", errors="ignore") as fh:
                        fh.seek(pos)
                        new_content = fh.read()
                        pos = fh.tell()
                except Exception:
                    continue
                if not new_content:
                    continue
                for line in new_content.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if not send(obj):
                        return
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_POST(self) -> None:  # noqa: N802
        """
        Handle POST requests.

        Accepts both JSON body (Content-Type: application/json) and
        form-encoded body (fallback) so the UI can use fetch with JSON
        while curl users can also use -d 'text=...' form syntax.
        """
        if self._check_rate_limit():
            return

        parsed = urlparse(self.path)
        ip = self._get_client_ip()

        # Read body (shared across all POST endpoints)
        MAX_CONTENT_LENGTH = 1_000_000  # 1 MB
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_CONTENT_LENGTH:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Payload too large"})
            return
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        data: dict[str, Any] = {}
        if raw:
            try:
                parsed_body = json.loads(raw)
                data = parsed_body if isinstance(parsed_body, dict) else {}
            except Exception:
                data = {k: v[0] for k, v in parse_qs(raw).items()}

        # --- Login endpoint (exempt from auth + CSRF) ---
        if parsed.path == "/api/login":
            password = str(data.get("password", ""))
            if not password:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "Missing password"})
                return

            # Try admin first, then viewer
            user_id = ""
            role = ""
            if _ADMIN_HASH and verify_password(password, _ADMIN_HASH):
                user_id, role = "admin", "admin"
            elif _VIEWER_HASH and verify_password(password, _VIEWER_HASH):
                user_id, role = "viewer", "viewer"

            if not user_id:
                _audit_logger.log_login_attempt(ip=ip, success=False)
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "Invalid password"})
                return

            # Create session token + CSRF token
            session_token = create_session_token(
                user_id, role, _SESSION_SECRET, _SESSION_TTL,
            )
            csrf_token = generate_csrf_token(session_token, _SESSION_SECRET)

            # Build Set-Cookie header
            cookie_parts = [
                f"session={session_token}",
                "HttpOnly",
                "SameSite=Strict",
                "Path=/",
                f"Max-Age={_SESSION_TTL}",
            ]
            if _TLS_ENABLED:
                cookie_parts.append("Secure")

            _audit_logger.log_login_attempt(ip=ip, success=True, user=user_id)

            body = json.dumps(
                {"ok": True, "role": role, "csrf_token": csrf_token},
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", "; ".join(cookie_parts))
            self.send_header("Cache-Control", "no-store")
            self._send_cors_headers()
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(body)
            return

        # --- Logout endpoint (exempt from CSRF) ---
        if parsed.path == "/api/logout":
            cookie_parts = [
                "session=",
                "HttpOnly",
                "SameSite=Strict",
                "Path=/",
                "Max-Age=0",
            ]
            body = json.dumps({"ok": True}).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", "; ".join(cookie_parts))
            self.send_header("Cache-Control", "no-store")
            self._send_cors_headers()
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(body)
            return

        # --- Auth gate for all other POST endpoints ---
        identity = self._check_auth()
        if identity is None:
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "Authentication required"})
            return

        # CSRF validation
        session_token = self._get_session_cookie()
        if not self._check_csrf(session_token):
            self._json(HTTPStatus.FORBIDDEN, {"error": "CSRF token missing or invalid"})
            return

        # RBAC
        if not check_permission(identity["role"], "POST", parsed.path):
            self._json(HTTPStatus.FORBIDDEN, {"error": "Insufficient permissions"})
            return

        user = identity.get("user_id", "")

        if parsed.path == "/api/ask":
            prompt = str(data.get("text", "")).strip()
            if not prompt:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "Missing 'text' prompt"})
                return
            if len(prompt) > 10_000:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "Prompt too long (max 10,000 chars)"})
                return
            meta: dict[str, Any] = {"kind": "freeform", "use_llm": True}
            strategy = str(data.get("strategy", "")).strip()
            if strategy:
                meta["strategy"] = strategy
            msg = enqueue(prompt, meta=meta)
            self._json(HTTPStatus.OK, {"queued": "ask", "msg": msg})

        elif parsed.path == "/api/health":
            msg = enqueue("health_check", meta={"kind": "health_check", "include_raw": False})
            self._json(HTTPStatus.OK, {"queued": "health_check", "msg": msg})

        elif parsed.path == "/api/config":
            if not isinstance(data, dict):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "Expected JSON object"})
                return
            # Validate config values: max 500 chars, no control characters
            sanitized: dict[str, str] = {}
            for k, v in data.items():
                sv = str(v)
                if len(sv) > 500:
                    continue
                # Reject control characters (except normal whitespace)
                if any(ord(c) < 32 and c not in ('\n', '\r', '\t') for c in sv):
                    continue
                sanitized[k] = sv
            _write_config(sanitized)
            saved_keys = [k for k in sanitized if k in _CONFIG_KEYS]
            _audit_logger.log_config_change(user=user, keys=saved_keys, ip=ip)
            self._json(HTTPStatus.OK, {"saved": saved_keys})

        elif parsed.path == "/api/restart_agent":
            restart_script = REPO_ROOT / "scripts" / "restart_agent.sh"
            if not restart_script.exists():
                self._json(HTTPStatus.NOT_FOUND, {"error": "restart_agent.sh not found"})
                return
            _audit_logger.log_admin_action(user=user, action="restart_agent", ip=ip)
            self._json(HTTPStatus.OK, {"status": "restart_initiated"})

            def _do_restart_agent() -> None:
                time.sleep(0.3)
                try:
                    subprocess.Popen(
                        ["bash", str(restart_script)],
                        cwd=str(REPO_ROOT),
                        start_new_session=True,
                    )
                except Exception as exc:
                    print(f"[ERROR] Failed to launch restart_agent.sh: {exc}", flush=True)

            threading.Thread(target=_do_restart_agent, daemon=True).start()

        elif parsed.path == "/api/shutdown":
            shutdown_script = REPO_ROOT / "scripts" / "shutdown.sh"
            if not shutdown_script.exists():
                self._json(HTTPStatus.NOT_FOUND, {"error": "shutdown.sh not found"})
                return
            _audit_logger.log_admin_action(user=user, action="shutdown", ip=ip)
            self._json(HTTPStatus.OK, {"status": "shutdown_initiated"})

            def _do_shutdown() -> None:
                # Small delay ensures the HTTP response flushes before shutdown
                # kills the UI server process.
                time.sleep(0.5)
                try:
                    subprocess.Popen(
                        ["bash", str(shutdown_script)],
                        cwd=str(REPO_ROOT),
                        # Detach from the current process group so it survives
                        # even if the UI server dies first.
                        start_new_session=True,
                    )
                except Exception as exc:
                    print(f"[ERROR] Failed to launch shutdown.sh: {exc}", flush=True)

            threading.Thread(target=_do_shutdown, daemon=True).start()

        elif parsed.path == "/api/restart":
            shutdown_script = REPO_ROOT / "scripts" / "shutdown.sh"
            start_script    = REPO_ROOT / "scripts" / "1.start.sh"
            if not shutdown_script.exists() or not start_script.exists():
                self._json(HTTPStatus.NOT_FOUND, {"error": "shutdown.sh or 1.start.sh not found"})
                return
            _audit_logger.log_admin_action(user=user, action="restart", ip=ip)
            self._json(HTTPStatus.OK, {"status": "restart_initiated"})

            def _do_restart() -> None:
                time.sleep(0.5)
                try:
                    # Run shutdown then start as one detached sequence.
                    # start_new_session=True ensures this process survives
                    # the UI server being killed by shutdown.sh.
                    subprocess.Popen(
                        ["bash", "-c",
                         f"bash '{shutdown_script}' && bash '{start_script}'"],
                        cwd=str(REPO_ROOT),
                        start_new_session=True,
                    )
                except Exception as exc:
                    print(f"[ERROR] Failed to launch restart sequence: {exc}", flush=True)

            threading.Thread(target=_do_restart, daemon=True).start()

        elif parsed.path == "/api/fresh":
            restart_agent = REPO_ROOT / "scripts" / "restart_agent.sh"
            if not restart_agent.exists():
                self._json(HTTPStatus.NOT_FOUND, {"error": "restart_agent.sh not found"})
                return
            _audit_logger.log_admin_action(user=user, action="fresh_restart", ip=ip)
            self._json(HTTPStatus.OK, {"status": "fresh_restart_initiated"})

            def _do_fresh() -> None:
                time.sleep(0.5)
                try:
                    subprocess.Popen(
                        ["bash", str(restart_agent), "fresh"],
                        cwd=str(REPO_ROOT),
                        start_new_session=True,
                    )
                except Exception as exc:
                    print(f"[ERROR] Failed to launch restart_agent.sh fresh: {exc}", flush=True)

            threading.Thread(target=_do_fresh, daemon=True).start()

        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Unknown endpoint"})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """
    Start the HTTP server.

    UI_HOST defaults to 127.0.0.1 (localhost only) for security.
    Set UI_HOST=0.0.0.0 to expose on all interfaces (e.g. in a container).
    UI_PORT defaults to 8008.

    ThreadingHTTPServer spawns a new thread per request, which allows the
    SSE endpoint to block without preventing other API calls from being served.

    TLS: when UI_TLS_CERT and UI_TLS_KEY are set, the server wraps its socket
    with an SSL context and serves HTTPS instead of HTTP.
    """
    host = os.getenv("UI_HOST", "127.0.0.1")
    port = int(os.getenv("UI_PORT", "8008"))
    server = ThreadingHTTPServer((host, port), UIHandler)

    # TLS support — wrap socket when cert/key paths are configured.
    tls_cert = os.getenv("UI_TLS_CERT", "")
    tls_key = os.getenv("UI_TLS_KEY", "")
    protocol = "http"
    if tls_cert and tls_key:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(tls_cert, tls_key)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        protocol = "https"

    auth_status = "enabled" if _AUTH_ENABLED else "disabled"
    print(json.dumps({
        "kind": "ui_server_start",
        "url": f"{protocol}://{host}:{port}",
        "auth": auth_status,
        "tls": protocol == "https",
    }), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
