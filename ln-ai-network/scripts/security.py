"""Security utilities for the Lightning Agent web UI.

Provides authentication (PBKDF2-SHA256 password hashing, HMAC-signed session
tokens), CSRF protection, per-IP HTTP rate limiting, RBAC role checking, and
security audit logging.

All primitives use Python stdlib only (hashlib, hmac, secrets, http.cookies)
so no additional dependencies are needed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Password hashing (PBKDF2-SHA256)
# ---------------------------------------------------------------------------

# OWASP 2023 recommendation for PBKDF2-SHA256
_PBKDF2_ITERATIONS = 600_000
_SALT_BYTES = 16


def hash_password(password: str) -> str:
    """Hash a password with a random salt. Returns ``salt_hex:hash_hex``."""
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    )
    return salt.hex() + ":" + dk.hex()


def verify_password(password: str, stored: str) -> bool:
    """Verify *password* against a ``salt_hex:hash_hex`` string."""
    try:
        salt_hex, hash_hex = stored.split(":", 1)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    )
    return hmac.compare_digest(dk, expected)


# ---------------------------------------------------------------------------
# Session tokens (HMAC-signed)
# ---------------------------------------------------------------------------


def create_session_token(user_id: str, role: str, secret: str, ttl: int = 3600) -> str:
    """Create an HMAC-signed session token: ``user_id:role:expiry_ts:signature``."""
    expiry = int(time.time()) + ttl
    payload = f"{user_id}:{role}:{expiry}"
    sig = hmac.new(
        secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{payload}:{sig}"


def validate_session_token(token: str, secret: str) -> dict[str, str] | None:
    """Validate a session token.

    Returns ``{"user_id": ..., "role": ...}`` if valid, ``None`` if expired or
    tampered.
    """
    try:
        parts = token.split(":")
        if len(parts) != 4:
            return None
        user_id, role, expiry_str, sig = parts
        expiry = int(expiry_str)
    except (ValueError, AttributeError):
        return None

    if time.time() > expiry:
        return None

    payload = f"{user_id}:{role}:{expiry_str}"
    expected_sig = hmac.new(
        secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return None

    return {"user_id": user_id, "role": role}


# ---------------------------------------------------------------------------
# CSRF tokens
# ---------------------------------------------------------------------------


def generate_csrf_token(session_token: str, secret: str) -> str:
    """Generate a CSRF token tied to the current session."""
    return hmac.new(
        secret.encode("utf-8"),
        f"csrf:{session_token}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def validate_csrf_token(csrf_token: str, session_token: str, secret: str) -> bool:
    """Validate a CSRF token against the session."""
    expected = generate_csrf_token(session_token, secret)
    return hmac.compare_digest(csrf_token, expected)


# ---------------------------------------------------------------------------
# RBAC — role-based access control
# ---------------------------------------------------------------------------

ROLES: dict[str, frozenset[str]] = {
    "admin": frozenset({"read", "write", "config", "system"}),
    "viewer": frozenset({"read"}),
}

# Maps "METHOD /path" to the required permission.
ENDPOINT_PERMISSIONS: dict[str, str] = {
    "GET /api/status": "read",
    "GET /api/pipeline_result": "read",
    "GET /api/trace": "read",
    "GET /api/network": "read",
    "GET /api/logs": "read",
    "GET /api/metrics": "read",
    "GET /api/crash_kit": "read",
    "GET /api/config": "read",
    "GET /api/stream": "read",
    "GET /api/tokens": "read",
    "POST /api/ask": "write",
    "POST /api/health": "write",
    "POST /api/config": "config",
    "POST /api/restart_agent": "system",
    "POST /api/shutdown": "system",
    "POST /api/restart": "system",
    "POST /api/fresh": "system",
}


def check_permission(role: str, method: str, path: str) -> bool:
    """Return True if *role* has permission for *method* + *path*."""
    # Normalize path: strip query string, strip trailing /api/logs/<file> to /api/logs
    clean = path.split("?")[0]
    if clean.startswith("/api/logs/"):
        clean = "/api/logs"

    key = f"{method.upper()} {clean}"
    required = ENDPOINT_PERMISSIONS.get(key)
    if required is None:
        # Unknown endpoint — allow (static files, OPTIONS, etc.)
        return True
    perms = ROLES.get(role, frozenset())
    return required in perms


# ---------------------------------------------------------------------------
# HTTP rate limiting (per-IP sliding window)
# ---------------------------------------------------------------------------

class HTTPRateLimiter:
    """Thread-safe per-IP sliding-window rate limiter."""

    def __init__(
        self,
        global_rpm: int = 100,
        sensitive_rpm: int = 10,
        login_rpm: int = 5,
        window_s: int = 60,
    ) -> None:
        self._global_rpm = global_rpm
        self._sensitive_rpm = sensitive_rpm
        self._login_rpm = login_rpm
        self._window_s = window_s
        # {ip: [(timestamp, endpoint_category), ...]}
        self._requests: dict[str, list[tuple[float, str]]] = {}
        self._lock = threading.Lock()
        self._call_count = 0

    _SENSITIVE_PATHS = frozenset({
        "/api/login", "/api/config", "/api/ask",
    })

    def check(self, ip: str, method: str, path: str) -> int | None:
        """Check rate limit. Returns ``None`` if allowed, or a ``Retry-After``
        value in seconds if the request should be rejected (HTTP 429)."""
        now = time.time()
        cutoff = now - self._window_s
        category = self._categorize(method, path)

        with self._lock:
            self._call_count += 1
            # Periodic prune every 100 requests
            if self._call_count % 100 == 0:
                self._prune(cutoff)

            entries = self._requests.get(ip, [])
            # Remove stale entries for this IP
            entries = [(t, c) for t, c in entries if t > cutoff]

            # Check limits
            global_count = len(entries)
            if global_count >= self._global_rpm:
                self._requests[ip] = entries
                return self._window_s

            if category == "login":
                login_count = sum(1 for _, c in entries if c == "login")
                if login_count >= self._login_rpm:
                    self._requests[ip] = entries
                    return self._window_s

            if category in ("login", "sensitive"):
                sensitive_count = sum(
                    1 for _, c in entries if c in ("login", "sensitive")
                )
                if sensitive_count >= self._sensitive_rpm:
                    self._requests[ip] = entries
                    return self._window_s

            entries.append((now, category))
            self._requests[ip] = entries
            return None

    def _categorize(self, method: str, path: str) -> str:
        clean = path.split("?")[0]
        if clean == "/api/login" and method.upper() == "POST":
            return "login"
        if clean in self._SENSITIVE_PATHS and method.upper() == "POST":
            return "sensitive"
        return "general"

    def _prune(self, cutoff: float) -> None:
        """Remove entries older than cutoff. Called under lock."""
        stale = [ip for ip, entries in self._requests.items()
                 if not entries or entries[-1][0] < cutoff]
        for ip in stale:
            del self._requests[ip]


# ---------------------------------------------------------------------------
# Security audit logging
# ---------------------------------------------------------------------------

class SecurityAuditLogger:
    """Append-only JSONL audit log for security events."""

    def __init__(self, log_path: Path | None = None) -> None:
        self._path = log_path
        self._lock = threading.Lock()

    def _write(self, entry: dict[str, Any]) -> None:
        if self._path is None:
            return
        entry["ts"] = time.time()
        line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
            except OSError:
                pass  # Non-fatal — never crash the server for audit logging

    def log_request(
        self,
        *,
        method: str,
        path: str,
        ip: str,
        user: str = "",
        status: int = 200,
        detail: str = "",
    ) -> None:
        self._write({
            "event": "request",
            "method": method,
            "path": path,
            "ip": ip,
            "user": user,
            "status": status,
            "detail": detail,
        })

    def log_login_attempt(self, *, ip: str, success: bool, user: str = "") -> None:
        self._write({
            "event": "login_attempt",
            "ip": ip,
            "success": success,
            "user": user,
        })

    def log_config_change(self, *, user: str, keys: list[str], ip: str = "") -> None:
        self._write({
            "event": "config_change",
            "user": user,
            "keys_changed": keys,
            "ip": ip,
        })

    def log_admin_action(self, *, user: str, action: str, ip: str = "") -> None:
        self._write({
            "event": "admin_action",
            "user": user,
            "action": action,
            "ip": ip,
        })
