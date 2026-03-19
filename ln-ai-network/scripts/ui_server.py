"""Lightning Network AI Agent — Web UI Server (v2)

Serves static files from web/ and exposes API endpoints that expose the full
3-stage pipeline state: translator intent, planner steps, executor results,
live trace log, and network topology.
"""
from __future__ import annotations

import json
import os
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = REPO_ROOT / "web"
RUNTIME_DIR = REPO_ROOT / "runtime" / "agent"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ai.command_queue import enqueue, last_outbox, paths


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def _read_jsonl_tail(path: Path, limit: int = 20) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    lines = [ln for ln in path.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()]
    out: list[dict[str, Any]] = []
    for ln in lines[-limit:]:
        try:
            obj = json.loads(ln)
        except Exception:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _read_trace_tail(limit: int = 150) -> list[dict[str, Any]]:
    return _read_jsonl_tail(RUNTIME_DIR / "trace.log", limit=limit)


# ---------------------------------------------------------------------------
# Pipeline result extraction
# ---------------------------------------------------------------------------

def _latest_pipeline_result() -> dict[str, Any] | None:
    """Return the most recent outbox entry that looks like a pipeline report."""
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
    """Try to unwrap nested tool result payloads into a flat dict."""
    if not isinstance(raw, dict):
        try:
            raw = json.loads(raw) if isinstance(raw, str) else {}
        except Exception:
            return {}
    # Common nesting patterns from MCP tool results
    for key in ("result", "payload", "data"):
        if key in raw and isinstance(raw[key], dict):
            inner = raw[key]
            # One more level: result.payload
            if "payload" in inner and isinstance(inner["payload"], dict):
                return inner["payload"]
            return inner
    return raw


def _extract_network_data() -> dict[str, Any]:
    """Scan recent outbox pipeline results for node/channel data from tool calls."""
    qp = paths()
    outbox_entries = _read_jsonl_tail(qp.outbox, limit=50)

    nodes: list[dict] = []
    channels: list[dict] = []

    for entry in reversed(outbox_entries):
        step_results = entry.get("step_results", [])
        for sr in step_results:
            tool = sr.get("tool", "")
            payload = _unwrap_payload(sr.get("raw_result", {}))

            if tool in ("ln_listnodes", "network_health"):
                candidates = (
                    payload.get("nodes")
                    or payload.get("nodelist")
                    or []
                )
                if isinstance(candidates, list) and candidates:
                    nodes = candidates

            if tool == "ln_listchannels":
                candidates = payload.get("channels") or []
                if isinstance(candidates, list) and candidates:
                    channels = candidates

            if tool == "ln_listpeers":
                for peer in payload.get("peers", []):
                    for ch in peer.get("channels", []):
                        channels.append(ch)

        if nodes or channels:
            break

    return {"nodes": nodes, "channels": channels}


# ---------------------------------------------------------------------------
# Runtime snapshot
# ---------------------------------------------------------------------------

def _runtime_snapshot() -> dict[str, Any]:
    qp = paths()
    inbox = _read_jsonl_tail(qp.inbox, limit=10)
    outbox = _read_jsonl_tail(qp.outbox, limit=10)
    lock_text = ""
    lock_path = qp.base_dir / "pipeline.lock"
    if not lock_path.exists():
        lock_path = qp.base_dir / "agent.lock"
    if lock_path.exists():
        lock_text = lock_path.read_text(encoding="utf-8", errors="ignore").strip()
    return {
        "agent_lock": lock_text,
        "last_outbox": last_outbox(),
        "recent_inbox": inbox,
        "recent_outbox": outbox,
        "message_count": len(inbox),
    }


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class UIHandler(BaseHTTPRequestHandler):
    server_version = "LightningAgentUI/2.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # suppress default access log

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, rel_path: str) -> None:
        rel = "index.html" if rel_path in ("", "/") else rel_path.lstrip("/")
        file_path = (WEB_ROOT / rel).resolve()
        web_root_resolved = WEB_ROOT.resolve()
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
        else:
            self._serve_static(parsed.path)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        data: dict[str, Any] = {}
        if raw:
            try:
                data = json.loads(raw)
            except Exception:
                data = {k: v[-1] for k, v in parse_qs(raw).items()}

        if parsed.path == "/api/ask":
            prompt = str(data.get("text", "")).strip()
            if not prompt:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "Missing 'text' prompt"})
                return
            msg = enqueue(prompt, meta={"kind": "freeform", "use_llm": True})
            self._json(HTTPStatus.OK, {"queued": "ask", "msg": msg})

        elif parsed.path == "/api/health":
            msg = enqueue("health_check", meta={"kind": "health_check", "include_raw": False})
            self._json(HTTPStatus.OK, {"queued": "health_check", "msg": msg})

        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Unknown endpoint"})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
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
