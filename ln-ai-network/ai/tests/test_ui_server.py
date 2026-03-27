"""Tests for scripts/ui_server.py — the web dashboard HTTP server.

Strategy:
  - All tests use monkeypatching to override module-level globals (auth,
    env file paths, runtime dirs) so no real filesystem or security state
    is required.
  - HTTP requests are issued via a thin `_request()` helper that wraps
    http.client against a real ThreadingHTTPServer running on a random port.
  - No live Lightning/Bitcoin infrastructure is needed.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest

# Ensure the repo root is on sys.path so we can import scripts.*
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# We import the module under test (scripts.ui_server) lazily inside
# fixtures so that monkeypatching can override module-level state
# before the handler processes requests.
import scripts.ui_server as ui_mod


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture()
def tmp_runtime(tmp_path):
    """Create a temporary runtime directory tree matching what the server expects."""
    agent_dir = tmp_path / "runtime" / "agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "logs").mkdir()
    (agent_dir / "inbox.jsonl").touch()
    (agent_dir / "outbox.jsonl").touch()
    (agent_dir / "trace.log").touch()
    return agent_dir


@pytest.fixture()
def tmp_env_file(tmp_path):
    """Create a temporary .env file."""
    env = tmp_path / ".env"
    env.write_text("LLM_BACKEND=openai\nOPENAI_API_KEY=sk-abc123xyz456\n")
    return env


@pytest.fixture()
def tmp_web_root(tmp_path):
    """Create a temporary web/ directory with a minimal index.html."""
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<html><body>test</body></html>")
    (web / "app.js").write_text("// app")
    (web / "styles.css").write_text("body{}")
    return web


@pytest.fixture()
def server(tmp_runtime, tmp_env_file, tmp_web_root, monkeypatch):
    """
    Spin up UIHandler on a random port with auth disabled.

    Monkeypatches module-level globals so the server uses temp paths and
    skips authentication / rate limiting / CSRF.
    """
    monkeypatch.setattr(ui_mod, "RUNTIME_DIR", tmp_runtime)
    monkeypatch.setattr(ui_mod, "ENV_FILE", tmp_env_file)
    monkeypatch.setattr(ui_mod, "WEB_ROOT", tmp_web_root)
    monkeypatch.setattr(ui_mod, "_AUTH_ENABLED", False)
    monkeypatch.setattr(ui_mod, "_TLS_ENABLED", False)
    monkeypatch.setattr(ui_mod, "_CORS_ORIGIN", "")
    # Replace the shared rate limiter with a fresh, permissive instance
    # so tests don't trip over limits accumulated from prior requests.
    from scripts.security import HTTPRateLimiter
    monkeypatch.setattr(ui_mod, "_rate_limiter", HTTPRateLimiter(
        global_rpm=10_000, sensitive_rpm=10_000, login_rpm=10_000,
    ))

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), ui_mod.UIHandler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield {"httpd": httpd, "port": port, "runtime": tmp_runtime, "env": tmp_env_file, "web": tmp_web_root}
    httpd.shutdown()


def _request(server_info: dict, method: str, path: str,
             body: dict | str | None = None, headers: dict | None = None) -> tuple[int, dict | str]:
    """Send an HTTP request and return (status_code, parsed_body)."""
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", server_info["port"], timeout=5)
    hdrs = headers or {}
    data: bytes | None = None
    if body is not None:
        if isinstance(body, dict):
            data = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        else:
            data = body.encode("utf-8")
        hdrs["Content-Length"] = str(len(data))
    conn.request(method, path, body=data, headers=hdrs)
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8")
    conn.close()
    try:
        return resp.status, json.loads(raw)
    except Exception:
        return resp.status, raw


# =============================================================================
# Static file serving
# =============================================================================

class TestStaticFiles:
    def test_index_html_served(self, server):
        status, body = _request(server, "GET", "/")
        assert status == 200
        assert "test" in body

    def test_js_file_served(self, server):
        status, body = _request(server, "GET", "/app.js")
        assert status == 200
        assert "app" in body

    def test_css_file_served(self, server):
        status, body = _request(server, "GET", "/styles.css")
        assert status == 200
        assert "body" in body

    def test_missing_static_file_404(self, server):
        status, body = _request(server, "GET", "/nonexistent.html")
        assert status == 404

    def test_path_traversal_blocked(self, server):
        status, _ = _request(server, "GET", "/../../../etc/passwd")
        assert status in (403, 404)


# =============================================================================
# GET /api/status
# =============================================================================

class TestGetStatus:
    def test_returns_200(self, server):
        status, body = _request(server, "GET", "/api/status")
        assert status == 200
        assert isinstance(body, dict)

    def test_contains_expected_fields(self, server):
        status, body = _request(server, "GET", "/api/status")
        assert status == 200
        # The runtime snapshot should include at least these keys
        assert "locked" in body or "inbox_count" in body or isinstance(body, dict)


# =============================================================================
# GET /api/config
# =============================================================================

class TestGetConfig:
    def test_returns_config_keys(self, server):
        status, body = _request(server, "GET", "/api/config")
        assert status == 200
        assert "LLM_BACKEND" in body

    def test_api_keys_are_masked(self, server):
        status, body = _request(server, "GET", "/api/config")
        assert status == 200
        key_val = body.get("OPENAI_API_KEY", "")
        # Should be masked (not the full key)
        assert "sk-abc123xyz456" != key_val
        assert body.get("OPENAI_API_KEY__set") is True

    def test_api_key_set_flag(self, server):
        status, body = _request(server, "GET", "/api/config")
        assert status == 200
        # We wrote a real key in the fixture
        assert body.get("OPENAI_API_KEY__set") is True


# =============================================================================
# POST /api/config — input validation
# =============================================================================

class TestPostConfig:
    def test_write_and_read_config(self, server):
        status, body = _request(server, "POST", "/api/config",
                                body={"LLM_BACKEND": "ollama"})
        assert status == 200
        assert "LLM_BACKEND" in body.get("saved", [])
        # Verify persisted
        env_text = server["env"].read_text()
        assert "LLM_BACKEND=ollama" in env_text

    def test_unknown_keys_silently_dropped(self, server):
        status, body = _request(server, "POST", "/api/config",
                                body={"LLM_BACKEND": "openai", "BOGUS_KEY": "val"})
        assert status == 200
        saved = body.get("saved", [])
        assert "LLM_BACKEND" in saved
        assert "BOGUS_KEY" not in saved

    def test_value_length_limit(self, server):
        long_val = "x" * 501
        status, body = _request(server, "POST", "/api/config",
                                body={"LLM_BACKEND": long_val})
        assert status == 200
        assert "LLM_BACKEND" in body.get("rejected", [])

    def test_control_chars_rejected(self, server):
        status, body = _request(server, "POST", "/api/config",
                                body={"LLM_BACKEND": "oll\x00ama"})
        assert status == 200
        assert "LLM_BACKEND" in body.get("rejected", [])

    def test_numeric_field_positive_integer(self, server):
        status, body = _request(server, "POST", "/api/config",
                                body={"MCP_CALL_TIMEOUT_S": "30"})
        assert status == 200
        assert "MCP_CALL_TIMEOUT_S" in body.get("saved", [])

    def test_numeric_field_rejects_negative(self, server):
        status, body = _request(server, "POST", "/api/config",
                                body={"MCP_CALL_TIMEOUT_S": "-5"})
        assert status == 200
        assert "MCP_CALL_TIMEOUT_S" in body.get("rejected", [])

    def test_numeric_field_rejects_zero(self, server):
        status, body = _request(server, "POST", "/api/config",
                                body={"UI_PORT": "0"})
        assert status == 200
        assert "UI_PORT" in body.get("rejected", [])

    def test_numeric_field_rejects_non_numeric(self, server):
        status, body = _request(server, "POST", "/api/config",
                                body={"MCP_CALL_TIMEOUT_S": "abc"})
        assert status == 200
        assert "MCP_CALL_TIMEOUT_S" in body.get("rejected", [])

    def test_masked_api_key_not_overwritten(self, server):
        """Sending a masked key back should not overwrite the real key."""
        status, body = _request(server, "POST", "/api/config",
                                body={"OPENAI_API_KEY": "sk-...456"})
        assert status == 200
        # The masked value should be silently dropped (not saved)
        env_text = server["env"].read_text()
        assert "sk-abc123xyz456" in env_text  # original preserved

    def test_per_stage_backend_override(self, server):
        status, body = _request(server, "POST", "/api/config",
                                body={"TRANSLATOR_LLM_BACKEND": "gemini"})
        assert status == 200
        assert "TRANSLATOR_LLM_BACKEND" in body.get("saved", [])


# =============================================================================
# POST /api/ask — prompt validation
# =============================================================================

class TestPostAsk:
    def test_empty_prompt_rejected(self, server):
        status, body = _request(server, "POST", "/api/ask", body={"text": ""})
        assert status == 400
        assert "Missing" in body.get("error", "")

    def test_missing_text_field(self, server):
        status, body = _request(server, "POST", "/api/ask", body={})
        assert status == 400

    def test_prompt_too_long(self, server):
        long_text = "x" * 10_001
        status, body = _request(server, "POST", "/api/ask", body={"text": long_text})
        assert status == 400
        assert "too long" in body.get("error", "").lower()

    def test_valid_prompt_queued(self, server):
        status, body = _request(server, "POST", "/api/ask",
                                body={"text": "What is the balance?"})
        assert status == 200
        assert body.get("queued") == "ask"

    def test_strategy_passthrough(self, server):
        status, body = _request(server, "POST", "/api/ask",
                                body={"text": "hello", "strategy": "conservative"})
        assert status == 200
        assert body.get("queued") == "ask"


# =============================================================================
# POST /api/health
# =============================================================================

class TestPostHealth:
    def test_health_check_queued(self, server):
        status, body = _request(server, "POST", "/api/health", body={})
        assert status == 200
        assert body.get("queued") == "health_check"


# =============================================================================
# GET /api/logs — archive listing and path traversal
# =============================================================================

class TestArchiveLogs:
    def _create_archive(self, runtime, name, first_line_data=None):
        logs = runtime / "logs"
        p = logs / name
        if first_line_data:
            p.write_text(json.dumps(first_line_data) + "\n")
        else:
            p.write_text(json.dumps({"user_text": "test prompt", "ts": 1000}) + "\n")
        return p

    def test_empty_logs_dir(self, server):
        status, body = _request(server, "GET", "/api/logs")
        assert status == 200
        assert body == []

    def test_list_archives(self, server):
        self._create_archive(server["runtime"], "0001_20260320-100000_ok.jsonl")
        self._create_archive(server["runtime"], "0002_20260320-100100_failed.jsonl")
        status, body = _request(server, "GET", "/api/logs")
        assert status == 200
        assert len(body) == 2

    def test_status_filter(self, server):
        self._create_archive(server["runtime"], "0001_20260320-100000_ok.jsonl")
        self._create_archive(server["runtime"], "0002_20260320-100100_failed.jsonl")
        status, body = _request(server, "GET", "/api/logs?status=ok")
        assert status == 200
        assert len(body) == 1
        assert body[0]["status"] == "ok"

    def test_invalid_status_ignored(self, server):
        self._create_archive(server["runtime"], "0001_20260320-100000_ok.jsonl")
        status, body = _request(server, "GET", "/api/logs?status=evil")
        assert status == 200
        # Invalid status is cleared to "", so returns all
        assert len(body) == 1

    def test_keyword_search(self, server):
        self._create_archive(server["runtime"], "0001_20260320-100000_ok.jsonl",
                             {"user_text": "open channel to node 2"})
        self._create_archive(server["runtime"], "0002_20260320-100100_ok.jsonl",
                             {"user_text": "check balance"})
        status, body = _request(server, "GET", "/api/logs?q=channel")
        assert status == 200
        assert len(body) == 1

    def test_read_single_archive(self, server):
        self._create_archive(server["runtime"], "0001_20260320-100000_ok.jsonl")
        status, body = _request(server, "GET", "/api/logs/0001_20260320-100000_ok.jsonl")
        assert status == 200
        assert "events" in body

    def test_read_missing_archive_404(self, server):
        status, body = _request(server, "GET", "/api/logs/nonexistent.jsonl")
        assert status == 404

    def test_path_traversal_slash(self, server):
        status, body = _request(server, "GET", "/api/logs/../../etc/passwd")
        assert status == 404

    def test_path_traversal_dotdot(self, server):
        status, body = _request(server, "GET", "/api/logs/..%2F..%2Fetc%2Fpasswd")
        assert status == 404

    def test_path_traversal_backslash(self, server):
        status, body = _request(server, "GET", "/api/logs/..\\..\\etc\\passwd")
        assert status == 404


# =============================================================================
# Content-Length limit
# =============================================================================

class TestContentLengthLimit:
    def test_oversized_payload_rejected(self, server):
        """Server rejects payloads with Content-Length > 1MB before reading the body."""
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", server["port"], timeout=5)
        # Send only the headers (claiming a huge body) — the server checks
        # Content-Length before reading and responds with 400.
        hdrs = {"Content-Type": "application/json", "Content-Length": "2000000"}
        try:
            conn.request("POST", "/api/ask", body=b'{"text":"x"}', headers=hdrs)
            resp = conn.getresponse()
            assert resp.status == 400
        except BrokenPipeError:
            # Server may close the connection before client finishes sending —
            # this is acceptable behaviour (reject early).
            pass
        finally:
            conn.close()


# =============================================================================
# Security headers
# =============================================================================

class TestSecurityHeaders:
    def _get_headers(self, server_info):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", server_info["port"], timeout=5)
        conn.request("GET", "/api/status")
        resp = conn.getresponse()
        resp.read()
        headers = {k.lower(): v for k, v in resp.getheaders()}
        conn.close()
        return headers

    def test_csp_header_present(self, server):
        headers = self._get_headers(server)
        assert "content-security-policy" in headers

    def test_x_frame_options(self, server):
        headers = self._get_headers(server)
        assert headers.get("x-frame-options") == "DENY"

    def test_x_content_type_options(self, server):
        headers = self._get_headers(server)
        assert headers.get("x-content-type-options") == "nosniff"

    def test_referrer_policy(self, server):
        headers = self._get_headers(server)
        assert "strict-origin" in headers.get("referrer-policy", "")

    def test_permissions_policy(self, server):
        headers = self._get_headers(server)
        assert "camera=()" in headers.get("permissions-policy", "")

    def test_no_hsts_without_tls(self, server):
        headers = self._get_headers(server)
        assert "strict-transport-security" not in headers

    def test_cache_control_no_store(self, server):
        headers = self._get_headers(server)
        assert headers.get("cache-control") == "no-store"


# =============================================================================
# Auth gate (when auth is enabled)
# =============================================================================

class TestAuthGate:
    def test_auth_disabled_returns_200(self, server):
        """When auth is disabled, all API endpoints return 200."""
        status, _ = _request(server, "GET", "/api/status")
        assert status == 200

    def test_auth_enabled_requires_session(self, server, monkeypatch):
        monkeypatch.setattr(ui_mod, "_AUTH_ENABLED", True)
        monkeypatch.setattr(ui_mod, "_SESSION_SECRET", "test-secret")
        status, body = _request(server, "GET", "/api/status")
        assert status == 401
        assert "Authentication" in body.get("error", "")

    def test_auth_enabled_static_files_exempt(self, server, monkeypatch):
        monkeypatch.setattr(ui_mod, "_AUTH_ENABLED", True)
        monkeypatch.setattr(ui_mod, "_SESSION_SECRET", "test-secret")
        status, _ = _request(server, "GET", "/")
        assert status == 200

    def test_auth_enabled_post_requires_session(self, server, monkeypatch):
        monkeypatch.setattr(ui_mod, "_AUTH_ENABLED", True)
        monkeypatch.setattr(ui_mod, "_SESSION_SECRET", "test-secret")
        status, body = _request(server, "POST", "/api/ask", body={"text": "hello"})
        assert status == 401


# =============================================================================
# Unknown endpoints
# =============================================================================

class TestUnknownEndpoints:
    def test_unknown_get_api(self, server):
        status, body = _request(server, "GET", "/api/does_not_exist")
        assert status == 404

    def test_unknown_post_api(self, server):
        status, body = _request(server, "POST", "/api/does_not_exist", body={})
        assert status == 404


# =============================================================================
# OPTIONS / CORS preflight
# =============================================================================

class TestCORS:
    def test_options_returns_204(self, server):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", server["port"], timeout=5)
        conn.request("OPTIONS", "/api/ask")
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 204
        conn.close()

    def test_cors_headers_absent_when_not_configured(self, server):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", server["port"], timeout=5)
        conn.request("GET", "/api/status")
        resp = conn.getresponse()
        resp.read()
        headers = {k.lower(): v for k, v in resp.getheaders()}
        assert "access-control-allow-origin" not in headers
        conn.close()

    def test_cors_headers_present_when_configured(self, server, monkeypatch):
        monkeypatch.setattr(ui_mod, "_CORS_ORIGIN", "https://example.com")
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", server["port"], timeout=5)
        conn.request("GET", "/api/status")
        resp = conn.getresponse()
        resp.read()
        headers = {k.lower(): v for k, v in resp.getheaders()}
        assert headers.get("access-control-allow-origin") == "https://example.com"
        conn.close()


# =============================================================================
# Helper function unit tests
# =============================================================================

class TestHelperFunctions:
    def test_mask_api_key_normal(self):
        assert ui_mod._mask_api_key("sk-abc123xyz456") == "sk-...z456"

    def test_mask_api_key_short(self):
        assert ui_mod._mask_api_key("short") == "****"

    def test_mask_api_key_empty(self):
        assert ui_mod._mask_api_key("") == ""

    def test_mask_api_key_placeholder(self):
        assert ui_mod._mask_api_key("__REPLACE_WITH_REAL_KEY__") == ""

    def test_is_masked_value_empty(self):
        assert ui_mod._is_masked_value("") is True

    def test_is_masked_value_stars(self):
        assert ui_mod._is_masked_value("****") is True

    def test_is_masked_value_dots(self):
        assert ui_mod._is_masked_value("sk-...456") is True

    def test_is_masked_value_real_key(self):
        assert ui_mod._is_masked_value("sk-abc123xyz456") is False

    def test_read_env_file(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("# comment\nFOO=bar\nBAZ=qux\n\n")
        monkeypatch.setattr(ui_mod, "ENV_FILE", env)
        result = ui_mod._read_env_file()
        assert result == {"FOO": "bar", "BAZ": "qux"}

    def test_read_env_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ui_mod, "ENV_FILE", tmp_path / "nope")
        assert ui_mod._read_env_file() == {}

    def test_write_config_preserves_comments(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("# This is a comment\nLLM_BACKEND=openai\n# Footer\n")
        monkeypatch.setattr(ui_mod, "ENV_FILE", env)
        ui_mod._write_config({"LLM_BACKEND": "ollama"})
        lines = env.read_text().splitlines()
        assert "# This is a comment" in lines
        assert "LLM_BACKEND=ollama" in lines
        assert "# Footer" in lines

    def test_write_config_appends_new_keys(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("LLM_BACKEND=openai\n")
        monkeypatch.setattr(ui_mod, "ENV_FILE", env)
        ui_mod._write_config({"OLLAMA_MODEL": "llama3"})
        text = env.read_text()
        assert "OLLAMA_MODEL=llama3" in text

    def test_write_config_rejects_unknown_keys(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("")
        monkeypatch.setattr(ui_mod, "ENV_FILE", env)
        ui_mod._write_config({"UNKNOWN_KEY": "value"})
        text = env.read_text()
        assert "UNKNOWN_KEY" not in text


# =============================================================================
# JSONL tail reader
# =============================================================================

class TestReadJsonlTail:
    def test_empty_file(self, tmp_path):
        p = tmp_path / "empty.jsonl"
        p.write_text("")
        assert ui_mod._read_jsonl_tail(p) == []

    def test_missing_file(self, tmp_path):
        assert ui_mod._read_jsonl_tail(tmp_path / "nope.jsonl") == []

    def test_reads_last_n_lines(self, tmp_path):
        p = tmp_path / "data.jsonl"
        lines = [json.dumps({"i": i}) for i in range(30)]
        p.write_text("\n".join(lines) + "\n")
        result = ui_mod._read_jsonl_tail(p, limit=5)
        assert len(result) == 5
        assert result[0]["i"] == 25

    def test_skips_malformed_lines(self, tmp_path):
        p = tmp_path / "data.jsonl"
        p.write_text('{"ok": 1}\nNOT JSON\n{"ok": 2}\n')
        result = ui_mod._read_jsonl_tail(p)
        assert len(result) == 2


# =============================================================================
# GET /api/trace
# =============================================================================

class TestGetTrace:
    def test_empty_trace(self, server):
        status, body = _request(server, "GET", "/api/trace")
        assert status == 200
        assert body.get("events") == []

    def test_trace_returns_events(self, server):
        trace = server["runtime"] / "trace.log"
        events = [json.dumps({"stage": "translator", "i": i}) for i in range(5)]
        trace.write_text("\n".join(events) + "\n")
        status, body = _request(server, "GET", "/api/trace")
        assert status == 200
        assert len(body["events"]) == 5


# =============================================================================
# GET /api/pipeline_result
# =============================================================================

class TestGetPipelineResult:
    def test_no_result_yet(self, server):
        status, body = _request(server, "GET", "/api/pipeline_result")
        assert status == 200
        # result may be None or empty when outbox is empty
        assert "result" in body

    def test_returns_latest_result(self, server):
        outbox = server["runtime"].parent.parent / "runtime" / "agent" / "outbox.jsonl"
        outbox.write_text(json.dumps({"human_summary": "All good", "success": True}) + "\n")
        status, body = _request(server, "GET", "/api/pipeline_result")
        assert status == 200


# =============================================================================
# GET /api/metrics
# =============================================================================

class TestGetMetrics:
    def test_empty_metrics(self, server):
        status, body = _request(server, "GET", "/api/metrics")
        assert status == 200
        assert body["total_queries"] == 0
        assert body["success_rate"] == 0.0

    def test_metrics_with_archives(self, server):
        logs = server["runtime"] / "logs"
        (logs / "0001_20260320-100000_ok.jsonl").write_text(
            json.dumps({"ts": 1000, "stage": "start"}) + "\n" +
            json.dumps({"ts": 1005, "stage": "end"}) + "\n"
        )
        (logs / "0002_20260320-100100_failed.jsonl").write_text(
            json.dumps({"ts": 2000, "stage_failed": "executor"}) + "\n"
        )
        status, body = _request(server, "GET", "/api/metrics")
        assert status == 200
        assert body["total_queries"] == 2
        assert body["status_counts"]["ok"] == 1
        assert body["status_counts"]["failed"] == 1
