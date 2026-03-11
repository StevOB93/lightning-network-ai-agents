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
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ai.command_queue import enqueue, last_outbox, paths


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


def _runtime_snapshot() -> dict[str, Any]:
    qp = paths()
    inbox = _read_jsonl_tail(qp.inbox, limit=10)
    outbox = _read_jsonl_tail(qp.outbox, limit=10)
    lock_text = ""
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


class DemoUIHandler(BaseHTTPRequestHandler):
    server_version = "LightningDemoUI/1.0"

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _text(self, status: int, body: str, content_type: str = "text/plain; charset=utf-8") -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, rel_path: str) -> None:
        rel = "index.html" if rel_path in ("", "/") else rel_path.lstrip("/")
        file_path = (WEB_ROOT / rel).resolve()
        if WEB_ROOT.resolve() not in file_path.parents and file_path != WEB_ROOT.resolve():
            self._text(HTTPStatus.FORBIDDEN, "Forbidden")
            return
        if not file_path.exists() or not file_path.is_file():
            self._text(HTTPStatus.NOT_FOUND, "Not found")
            return

        if file_path.suffix == ".html":
            ctype = "text/html; charset=utf-8"
        elif file_path.suffix == ".css":
            ctype = "text/css; charset=utf-8"
        elif file_path.suffix == ".js":
            ctype = "application/javascript; charset=utf-8"
        else:
            ctype = "application/octet-stream"

        self._text(HTTPStatus.OK, file_path.read_text(encoding="utf-8"), ctype)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            self._json(HTTPStatus.OK, _runtime_snapshot())
            return

        if parsed.path == "/api/last":
            self._json(HTTPStatus.OK, {"last_outbox": last_outbox()})
            return

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

        if parsed.path == "/api/health":
            msg = enqueue("health_check", meta={"kind": "health_check", "include_raw": False})
            self._json(HTTPStatus.OK, {"queued": "health_check", "msg": msg})
            return

        if parsed.path == "/api/ask":
            prompt = str(data.get("text", "")).strip()
            if not prompt:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "Missing 'text' prompt"})
                return
            msg = enqueue(prompt, meta={"kind": "freeform", "use_llm": True})
            self._json(HTTPStatus.OK, {"queued": "ask (LLM)", "msg": msg})
            return

        self._json(HTTPStatus.NOT_FOUND, {"error": "Unknown endpoint"})


def main() -> None:
    host = os.getenv("DEMO_UI_HOST", "127.0.0.1")
    port = int(os.getenv("DEMO_UI_PORT", "8008"))
    server = ThreadingHTTPServer((host, port), DemoUIHandler)
    print(json.dumps({"kind": "demo_ui_start", "url": f"http://{host}:{port}"}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
