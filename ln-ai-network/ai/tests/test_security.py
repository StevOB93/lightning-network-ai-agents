"""Tests for the security module (scripts/security.py).

Covers password hashing, session tokens, CSRF tokens, RBAC permission
checks, and the HTTP rate limiter. All tests are self-contained with no
external dependencies or live infrastructure.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path


# Ensure the repo root is on sys.path so we can import scripts.security
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.security import (
    HTTPRateLimiter,
    SecurityAuditLogger,
    check_permission,
    create_session_token,
    generate_csrf_token,
    hash_password,
    validate_csrf_token,
    validate_session_token,
    verify_password,
)


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

class TestPasswordHashing:
    def test_hash_and_verify(self):
        hashed = hash_password("mysecretpassword")
        assert verify_password("mysecretpassword", hashed)

    def test_wrong_password_fails(self):
        hashed = hash_password("correct_password")
        assert not verify_password("wrong_password", hashed)

    def test_hash_format(self):
        hashed = hash_password("test")
        parts = hashed.split(":")
        assert len(parts) == 2
        assert len(parts[0]) == 32  # 16 bytes hex = 32 chars
        assert len(parts[1]) == 64  # SHA256 = 32 bytes hex = 64 chars

    def test_different_salts(self):
        h1 = hash_password("same_password")
        h2 = hash_password("same_password")
        # Different salts should produce different hashes
        assert h1 != h2
        # But both should verify correctly
        assert verify_password("same_password", h1)
        assert verify_password("same_password", h2)

    def test_verify_empty_stored(self):
        assert not verify_password("test", "")

    def test_verify_malformed_stored(self):
        assert not verify_password("test", "not_a_valid_hash")

    def test_verify_invalid_hex(self):
        assert not verify_password("test", "ZZZZ:YYYY")

    def test_empty_password(self):
        hashed = hash_password("")
        assert verify_password("", hashed)
        assert not verify_password("notempty", hashed)


# ---------------------------------------------------------------------------
# Session tokens
# ---------------------------------------------------------------------------

class TestSessionTokens:
    SECRET = "test_secret_key_for_sessions"

    def test_create_and_validate(self):
        token = create_session_token("admin", "admin", self.SECRET, ttl=3600)
        result = validate_session_token(token, self.SECRET)
        assert result is not None
        assert result["user_id"] == "admin"
        assert result["role"] == "admin"

    def test_viewer_role(self):
        token = create_session_token("viewer", "viewer", self.SECRET)
        result = validate_session_token(token, self.SECRET)
        assert result is not None
        assert result["role"] == "viewer"

    def test_expired_token(self):
        token = create_session_token("admin", "admin", self.SECRET, ttl=0)
        # Token with 0 TTL expires immediately
        time.sleep(0.1)
        result = validate_session_token(token, self.SECRET)
        assert result is None

    def test_wrong_secret_fails(self):
        token = create_session_token("admin", "admin", self.SECRET)
        result = validate_session_token(token, "wrong_secret")
        assert result is None

    def test_tampered_token(self):
        token = create_session_token("admin", "admin", self.SECRET)
        # Tamper with the user_id
        parts = token.split(":")
        parts[0] = "hacker"
        tampered = ":".join(parts)
        result = validate_session_token(tampered, self.SECRET)
        assert result is None

    def test_malformed_token(self):
        assert validate_session_token("", self.SECRET) is None
        assert validate_session_token("abc", self.SECRET) is None
        assert validate_session_token("a:b:c", self.SECRET) is None
        assert validate_session_token("a:b:notanint:d", self.SECRET) is None

    def test_token_format(self):
        token = create_session_token("user1", "admin", self.SECRET, ttl=60)
        parts = token.split(":")
        assert len(parts) == 4
        assert parts[0] == "user1"
        assert parts[1] == "admin"
        # parts[2] is expiry timestamp (integer string)
        int(parts[2])
        # parts[3] is HMAC hex digest
        assert len(parts[3]) == 64


# ---------------------------------------------------------------------------
# CSRF tokens
# ---------------------------------------------------------------------------

class TestCSRFTokens:
    SECRET = "csrf_test_secret"

    def test_generate_and_validate(self):
        session = create_session_token("admin", "admin", self.SECRET)
        csrf = generate_csrf_token(session, self.SECRET)
        assert validate_csrf_token(csrf, session, self.SECRET)

    def test_wrong_session_fails(self):
        session1 = create_session_token("admin", "admin", self.SECRET, ttl=3600)
        session2 = create_session_token("admin", "admin", self.SECRET, ttl=7200)
        csrf = generate_csrf_token(session1, self.SECRET)
        # CSRF token tied to session1 should not validate against session2
        assert not validate_csrf_token(csrf, session2, self.SECRET)

    def test_wrong_secret_fails(self):
        session = create_session_token("admin", "admin", self.SECRET)
        csrf = generate_csrf_token(session, self.SECRET)
        assert not validate_csrf_token(csrf, session, "other_secret")

    def test_empty_token(self):
        session = create_session_token("admin", "admin", self.SECRET)
        assert not validate_csrf_token("", session, self.SECRET)

    def test_deterministic(self):
        session = "fixed_session_token"
        csrf1 = generate_csrf_token(session, self.SECRET)
        csrf2 = generate_csrf_token(session, self.SECRET)
        assert csrf1 == csrf2


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------

class TestRBAC:
    def test_admin_has_all_permissions(self):
        assert check_permission("admin", "GET", "/api/status")
        assert check_permission("admin", "POST", "/api/ask")
        assert check_permission("admin", "POST", "/api/config")
        assert check_permission("admin", "POST", "/api/shutdown")
        assert check_permission("admin", "POST", "/api/restart")
        assert check_permission("admin", "POST", "/api/fresh")

    def test_viewer_read_only(self):
        assert check_permission("viewer", "GET", "/api/status")
        assert check_permission("viewer", "GET", "/api/pipeline_result")
        assert check_permission("viewer", "GET", "/api/trace")
        assert check_permission("viewer", "GET", "/api/config")
        assert check_permission("viewer", "GET", "/api/stream")
        assert check_permission("viewer", "GET", "/api/tokens")

    def test_viewer_cannot_write(self):
        assert not check_permission("viewer", "POST", "/api/ask")
        assert not check_permission("viewer", "POST", "/api/health")

    def test_viewer_cannot_config(self):
        assert not check_permission("viewer", "POST", "/api/config")

    def test_viewer_cannot_system(self):
        assert not check_permission("viewer", "POST", "/api/shutdown")
        assert not check_permission("viewer", "POST", "/api/restart")
        assert not check_permission("viewer", "POST", "/api/restart_agent")
        assert not check_permission("viewer", "POST", "/api/fresh")

    def test_unknown_role_denied(self):
        assert not check_permission("unknown", "POST", "/api/ask")

    def test_unknown_endpoint_allowed(self):
        # Unknown endpoints (static files, OPTIONS) are allowed by default
        assert check_permission("viewer", "GET", "/index.html")
        assert check_permission("viewer", "GET", "/styles.css")

    def test_logs_subpath_normalized(self):
        assert check_permission("viewer", "GET", "/api/logs/somefile.jsonl")
        assert check_permission("admin", "GET", "/api/logs/somefile.jsonl")

    def test_query_string_stripped(self):
        assert check_permission("viewer", "GET", "/api/logs?q=test&status=ok")


# ---------------------------------------------------------------------------
# HTTP rate limiter
# ---------------------------------------------------------------------------

class TestHTTPRateLimiter:
    def test_allows_normal_requests(self):
        rl = HTTPRateLimiter(global_rpm=100)
        result = rl.check("1.2.3.4", "GET", "/api/status")
        assert result is None  # allowed

    def test_blocks_after_global_limit(self):
        rl = HTTPRateLimiter(global_rpm=3)
        for _ in range(3):
            assert rl.check("1.2.3.4", "GET", "/api/status") is None
        # 4th request should be blocked
        result = rl.check("1.2.3.4", "GET", "/api/status")
        assert result is not None
        assert result > 0

    def test_different_ips_independent(self):
        rl = HTTPRateLimiter(global_rpm=2)
        assert rl.check("1.1.1.1", "GET", "/api/status") is None
        assert rl.check("1.1.1.1", "GET", "/api/status") is None
        assert rl.check("1.1.1.1", "GET", "/api/status") is not None
        # Different IP should still be allowed
        assert rl.check("2.2.2.2", "GET", "/api/status") is None

    def test_login_rate_limit(self):
        rl = HTTPRateLimiter(global_rpm=100, login_rpm=2)
        assert rl.check("1.2.3.4", "POST", "/api/login") is None
        assert rl.check("1.2.3.4", "POST", "/api/login") is None
        # 3rd login should be blocked
        result = rl.check("1.2.3.4", "POST", "/api/login")
        assert result is not None

    def test_sensitive_rate_limit(self):
        rl = HTTPRateLimiter(global_rpm=100, sensitive_rpm=3, login_rpm=5)
        assert rl.check("1.2.3.4", "POST", "/api/config") is None
        assert rl.check("1.2.3.4", "POST", "/api/config") is None
        assert rl.check("1.2.3.4", "POST", "/api/config") is None
        # 4th sensitive request should be blocked
        result = rl.check("1.2.3.4", "POST", "/api/config")
        assert result is not None

    def test_get_not_sensitive(self):
        rl = HTTPRateLimiter(global_rpm=100, sensitive_rpm=1)
        # GET requests to sensitive endpoints are not rate-limited as sensitive
        assert rl.check("1.2.3.4", "GET", "/api/config") is None
        assert rl.check("1.2.3.4", "GET", "/api/config") is None


# ---------------------------------------------------------------------------
# Security audit logger
# ---------------------------------------------------------------------------

class TestSecurityAuditLogger:
    def test_log_to_file(self, tmp_path):
        log_file = tmp_path / "audit.jsonl"
        logger = SecurityAuditLogger(log_path=log_file)
        logger.log_login_attempt(ip="127.0.0.1", success=True, user="admin")
        assert log_file.exists()
        import json
        entry = json.loads(log_file.read_text().strip())
        assert entry["event"] == "login_attempt"
        assert entry["ip"] == "127.0.0.1"
        assert entry["success"] is True
        assert "ts" in entry

    def test_log_config_change(self, tmp_path):
        log_file = tmp_path / "audit.jsonl"
        logger = SecurityAuditLogger(log_path=log_file)
        logger.log_config_change(user="admin", keys=["LLM_BACKEND", "OPENAI_MODEL"], ip="10.0.0.1")
        import json
        entry = json.loads(log_file.read_text().strip())
        assert entry["event"] == "config_change"
        assert entry["keys_changed"] == ["LLM_BACKEND", "OPENAI_MODEL"]

    def test_log_admin_action(self, tmp_path):
        log_file = tmp_path / "audit.jsonl"
        logger = SecurityAuditLogger(log_path=log_file)
        logger.log_admin_action(user="admin", action="shutdown", ip="127.0.0.1")
        import json
        entry = json.loads(log_file.read_text().strip())
        assert entry["event"] == "admin_action"
        assert entry["action"] == "shutdown"

    def test_no_path_no_crash(self):
        logger = SecurityAuditLogger(log_path=None)
        # Should not raise
        logger.log_request(method="GET", path="/api/status", ip="127.0.0.1")

    def test_multiple_entries(self, tmp_path):
        log_file = tmp_path / "audit.jsonl"
        logger = SecurityAuditLogger(log_path=log_file)
        logger.log_login_attempt(ip="1.1.1.1", success=False)
        logger.log_login_attempt(ip="1.1.1.1", success=True, user="admin")
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_creates_parent_dirs(self, tmp_path):
        log_file = tmp_path / "subdir" / "deep" / "audit.jsonl"
        logger = SecurityAuditLogger(log_path=log_file)
        logger.log_request(method="GET", path="/", ip="127.0.0.1")
        assert log_file.exists()
