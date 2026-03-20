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
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

# Resolve repo root relative to this script's location
REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = REPO_ROOT / "web"
RUNTIME_DIR = REPO_ROOT / "runtime" / "agent"

# Ensure the repo root is on sys.path so we can import ai.* and mcp.*
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ai.command_queue import enqueue, last_outbox, paths
from mcp.ln_mcp_server import handle as mcp_handle


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def _read_jsonl_tail(path: Path, limit: int = 20) -> list[dict[str, Any]]:
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


def _read_trace_tail(limit: int = 150) -> list[dict[str, Any]]:
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
            "user_text_preview": user_text[:120],  # Truncated for the list view
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

        # Read the file to gather stage_failed events and timestamps
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
        except Exception:
            pass  # Unreadable file — skip it

        if len(timestamps) >= 2:
            durations.append(max(timestamps) - min(timestamps))

    success_rate = (status_counts.get("ok", 0) / total) if total > 0 else 0.0
    avg_duration_s = (sum(durations) / len(durations)) if durations else None

    return {
        "total_queries": total,
        "status_counts": status_counts,
        "success_rate": success_rate,
        "stage_failure_counts": stage_failure_counts,
        "avg_duration_s": avg_duration_s,
    }


def _read_archive(filename: str) -> dict[str, Any] | None:
    """
    Return all events from a single archived trace file.

    Security: rejects any filename containing path separators or ".." to prevent
    directory traversal attacks. The filename is then joined to the fixed
    logs_dir path so even a bypassed check couldn't escape the directory.

    Returns None (→ 404) if the file is missing or the filename is invalid.
    """
    # Reject path components that could escape the logs directory
    if "/" in filename or "\\" in filename or ".." in filename:
        return None
    p = RUNTIME_DIR / "logs" / filename
    if not p.exists() or not p.is_file():
        return None
    # limit=10000 effectively reads the whole file — archive files are bounded
    # by the duration of a single query (typically a few hundred events).
    return {"filename": filename, "events": _read_jsonl_tail(p, limit=10000)}


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

def _runtime_snapshot() -> dict[str, Any]:
    """
    Build a lightweight status snapshot for the status bar and queue panels.

    Reads the agent/pipeline lock file to determine if the backend process is
    running, and the last 10 inbox/outbox entries for the queue panels.

    Checks for pipeline.lock first (pipeline mode), then agent.lock (legacy mode).
    """
    qp = paths()
    inbox = _read_jsonl_tail(qp.inbox, limit=10)
    outbox = _read_jsonl_tail(qp.outbox, limit=10)

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
      /api/stream         → SSE stream of live updates
      /*                  → static files from web/

    POST endpoints:
      /api/ask            → enqueue a freeform prompt for the pipeline
      /api/health         → enqueue a health check ping
    """

    server_version = "LightningAgentUI/2.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # Suppress the default per-request access log to keep stdout clean

    def _json(self, status: int, payload: Any) -> None:
        """
        Send a JSON response with appropriate headers.

        default=str ensures non-serializable types (Path, datetime, etc.) are
        converted to strings rather than raising TypeError.

        Cache-Control: no-store prevents the browser from caching API responses,
        which would cause stale data to be shown after a pipeline run completes.

        CORS header allows the UI to be served from a different port during
        development (e.g. a hot-reload dev server on :3000 talking to API on :8008).
        """
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
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
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self) -> None:  # noqa: N802
        """
        Handle CORS preflight requests from the browser.
        Required for cross-origin POST requests (e.g. from a dev server).
        """
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path

        if route == "/api/status":
            self._json(HTTPStatus.OK, _runtime_snapshot())
        elif route == "/api/pipeline_result":
            self._json(HTTPStatus.OK, {"result": _latest_pipeline_result()})
        elif route == "/api/trace":
            self._json(HTTPStatus.OK, {"events": _read_trace_tail()})
        elif route == "/api/network":
            self._json(HTTPStatus.OK, _extract_network_data())
        elif route == "/api/logs":
            # Archive list — metadata only, no event content
            qs = parse_qs(parsed.query)
            q_param = qs.get("q", [""])[0]
            status_param = qs.get("status", [""])[0]
            self._json(HTTPStatus.OK, _list_archives(q=q_param, status=status_param))
        elif route.startswith("/api/logs/"):
            # Individual archive file — full event list
            filename = route[len("/api/logs/"):]
            result = _read_archive(filename)
            if result is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Archive not found"})
            else:
                self._json(HTTPStatus.OK, result)
        elif route == "/api/metrics":
            self._json(HTTPStatus.OK, _compute_metrics())
        elif route == "/api/stream":
            self._sse_stream()
        else:
            self._serve_static(parsed.path)

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
        self.send_header("Access-Control-Allow-Origin", "*")
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
        # Send last 50 trace events on connect (smaller than REST endpoint's 150)
        if not send("trace", {"events": _read_trace_tail(limit=50)}):
            return

        try:
            while True:
                time.sleep(0.4)  # 400ms poll interval — fast enough for real-time feel

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

    def do_POST(self) -> None:  # noqa: N802
        """
        Handle POST requests for /api/ask and /api/health.

        Accepts both JSON body (Content-Type: application/json) and
        form-encoded body (fallback) so the UI can use fetch with JSON
        while curl users can also use -d 'text=...' form syntax.
        """
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        data: dict[str, Any] = {}
        if raw:
            try:
                data = json.loads(raw)  # Prefer JSON body
            except Exception:
                # Fallback: parse as application/x-www-form-urlencoded
                # parse_qs returns lists; take the first value for each key
                data = {k: v[0] for k, v in parse_qs(raw).items()}

        if parsed.path == "/api/ask":
            prompt = str(data.get("text", "")).strip()
            if not prompt:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "Missing 'text' prompt"})
                return
            # Enqueue with use_llm=True to trigger the full 4-stage pipeline
            msg = enqueue(prompt, meta={"kind": "freeform", "use_llm": True})
            self._json(HTTPStatus.OK, {"queued": "ask", "msg": msg})

        elif parsed.path == "/api/health":
            # Health check: the pipeline processes this immediately and responds
            # with "Pipeline is running." — useful for monitoring scripts.
            msg = enqueue("health_check", meta={"kind": "health_check", "include_raw": False})
            self._json(HTTPStatus.OK, {"queued": "health_check", "msg": msg})

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
    """
    host = os.getenv("UI_HOST", "127.0.0.1")
    port = int(os.getenv("UI_PORT", "8008"))
    server = ThreadingHTTPServer((host, port), UIHandler)
    print(json.dumps({"kind": "ui_server_start", "url": f"http://{host}:{port}"}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
