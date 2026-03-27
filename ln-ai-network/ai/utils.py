from __future__ import annotations

# =============================================================================
# ai.utils — shared runtime utilities for agent.py and pipeline.py
#
# Both entry points (ai.agent and ai.pipeline) need identical implementations
# of StartupLock, TraceLogger, and a handful of small helper functions.
# This module is the single authoritative source; both files import from here.
#
# Contents:
#   StartupLock          — fcntl-based single-instance lock (Linux/WSL) with a
#                          plain-text fallback for Windows
#   TraceLogger          — per-query JSONL trace writer with reset/log/archive lifecycle
#   _repo_root()         — path to ln-ai-network/
#   _runtime_agent_dir() — path to runtime/agent/
#   _now_monotonic()     — time.monotonic() alias (keeps callers decoupled from time)
#   _env_bool()          — read a boolean env var ("1"/"true"/"yes"/"on")
#   _env_int()           — read an integer env var with a default
#   _env_float()         — read a float env var with a default
# =============================================================================

import atexit
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import fcntl  # Linux/WSL file-locking primitives
except Exception:
    fcntl = None  # type: ignore  # Not available on Windows


# =============================================================================
# Startup lock (single-instance enforcement)
# =============================================================================

class StartupLock:
    """
    Prevents two processes of the same type from running simultaneously against
    the same runtime directory, which would cause double-processing of inbox
    messages and corrupt the trace log.

    Strategy:
      - Linux/WSL: fcntl.flock (LOCK_EX | LOCK_NB) on the lock file.
        The OS releases the lock automatically when the process exits or crashes —
        no manual cleanup needed in the normal case.
      - Windows (no fcntl): reads the lock file and raises if it's non-empty.
        This is a best-effort fallback; a crash won't auto-release the lock,
        so the stale file must be deleted manually.

    The lock file stores "pid=<N> started_ts=<T>" for human diagnosis.

    Parameters:
      lock_path — path to the lock file (created if absent)
      name      — short name used in error JSON: "pipeline" → kind "pipeline_lock_failed"
    """

    def __init__(self, lock_path: Path, name: str = "process") -> None:
        self.lock_path = lock_path
        self._name = name
        self._fh = None  # Open file handle kept alive for the duration of the lock

    def acquire_or_exit(self) -> None:
        """Acquire the lock or terminate the process with a JSON error on stderr."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        # Open in append+read mode so we can seek and read the existing content
        # without truncating it before we know whether another process holds the lock.
        fh = self.lock_path.open("a+", encoding="utf-8")
        try:
            if fcntl is None:
                # Windows fallback: treat non-empty file as "locked"
                fh.seek(0)
                existing = fh.read().strip()
                if existing:
                    raise RuntimeError(existing)
            else:
                try:
                    # LOCK_NB causes immediate raise instead of blocking
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    # Another process holds the flock — read its PID and check
                    # whether that process is actually alive before failing.
                    # On some Linux kernels (and WSL2) a killed process may
                    # hold the flock briefly after death; if the PID no longer
                    # exists the lock is stale and safe to steal.
                    fh.seek(0)
                    existing = fh.read().strip()
                    stale = False
                    if existing:
                        try:
                            pid = int(existing.split()[0].replace("pid=", ""))
                            os.kill(pid, 0)  # signal 0 = existence check only
                        except (ValueError, ProcessLookupError):
                            stale = True  # PID gone — lock is stale
                        except PermissionError:
                            pass  # process exists but owned by another user
                    if stale:
                        # Clear the stale lock file and try to own it (non-blocking
                        # so two concurrent stealers don't deadlock each other).
                        print(
                            f"[StartupLock] stale lock detected (pid not running); "
                            f"stealing: {self.lock_path}",
                            flush=True,
                        )
                        fh.seek(0)
                        fh.truncate()
                        fh.flush()
                        try:
                            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            msg = "Another process stole the stale lock first."
                            raise RuntimeError(msg)
                    else:
                        msg = existing or f"Another {self._name} instance holds the lock."
                        raise RuntimeError(msg)

            # We own the lock — write our identity so operators can debug
            fh.seek(0)
            fh.truncate()
            fh.write(f"pid={os.getpid()} started_ts={int(time.time())}\n")
            fh.flush()
            try:
                os.fsync(fh.fileno())  # Ensure PID is on disk before we proceed
            except Exception:
                pass  # fsync may not be supported on all filesystems

            self._fh = fh  # Keep handle open — closing releases the flock
            atexit.register(self.release)  # Release cleanly on normal exit

        except Exception as e:
            try:
                fh.close()
            except Exception:
                pass
            # Emit structured JSON so the calling shell script can parse it
            err = {
                "kind": f"{self._name}_lock_failed",
                "lock_path": str(self.lock_path),
                "error": str(e),
                "hint": f"Another ai.{self._name} process is already running. Stop it first.",
            }
            print(json.dumps(err, ensure_ascii=False), file=sys.stderr)
            raise SystemExit(1)

    def release(self) -> None:
        """Unlock and close the lock file. Safe to call multiple times."""
        if self._fh is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass
        self._fh = None


# =============================================================================
# Trace logger (reset per prompt)
# =============================================================================

class TraceLogger:
    """
    Writes per-prompt execution traces to a single JSONL file (trace.log).

    Lifecycle per query:
      1. reset(header) — truncates trace.log and writes a prompt_start header.
         This signals the UI that a new query has started.
      2. log(event)*   — appends individual events during pipeline execution.
         Called by every stage (translator, planner, executor, summarizer).
      3. archive(...)  — called after the query completes to snapshot the full
         trace into a permanent archive file before the next reset.

    Every write is followed by flush() + fsync() to ensure the UI server
    (which reads the file from a separate process/thread) sees updates promptly.
    Without fsync the OS may buffer writes and the UI polling loop might miss
    events for several seconds.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def reset(self, header: Dict[str, Any]) -> None:
        """
        Start a new trace for a fresh query.

        Opens in 'w' mode (truncate) so the previous query's events are
        discarded from the live file. The first line written is the header
        dict — always a prompt_start event — which gives the archive() method
        the req_id and start timestamp without re-reading the file.
        """
        with self.path.open("w", encoding="utf-8") as f:
            f.write(json.dumps(header, ensure_ascii=False) + "\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass

    def log(self, event: Dict[str, Any]) -> None:
        """
        Append a single event to the live trace.

        Opens in 'a' mode each time rather than keeping the file open, so
        there's no risk of a missed flush if the process crashes mid-execution.
        The ts field is auto-inserted if the caller omits it, so callers can
        use short dicts like {"event": "step_start", "step_id": 3}.
        """
        event = dict(event)  # Shallow copy so we don't mutate the caller's dict
        event.setdefault("ts", int(time.time()))
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass

    def archive(self, req_id: int, start_ts: int, status: str) -> Optional[Path]:
        """
        Copy the completed trace into a permanent archive file.

        Called AFTER _write_report() so the archive contains all events
        including the final summarizer output and goal verification.

        Filename format: {req_id:04d}_{YYYYMMDD-HHMMSS}_{status}.jsonl
          - req_id zero-padded to 4 digits → lexicographic sort = chronological sort
          - start_ts used (not archive time) → filename reflects when the query began
          - status: "ok" | "partial" | "failed"

        Uses shutil.copy2 for an atomic snapshot of trace.log at this moment.
        Safe because log() always fsyncs before returning.

        Returns the destination Path on success, None on any error.
        Archiving is best-effort — a disk error here must never crash the pipeline.
        """
        import shutil
        from datetime import datetime, timezone
        logs_dir = self.path.parent / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        dt = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
        filename = f"{req_id:04d}_{dt}_{status}.jsonl"
        dest = logs_dir / filename
        try:
            shutil.copy2(self.path, dest)
        except Exception:
            return None
        return dest


# =============================================================================
# Path helpers
# =============================================================================

def _repo_root() -> Path:
    """Resolve the repo root two levels above this file (ln-ai-network/)."""
    return Path(__file__).resolve().parents[1]


def _runtime_agent_dir(agent_id: Optional[str] = None) -> Path:
    """Path to the agent's runtime directory.

    When agent_id is None (default), returns ``runtime/agent/`` for backward
    compatibility with single-agent mode.  When agent_id is set (e.g. "2"),
    returns ``runtime/agent-{agent_id}/`` for multi-agent mode.
    """
    if agent_id:
        return _repo_root() / "runtime" / f"agent-{agent_id}"
    return _repo_root() / "runtime" / "agent"


# =============================================================================
# Environment and time helpers
# =============================================================================

def _now_monotonic() -> float:
    """Monotonic clock in seconds (unaffected by NTP jumps)."""
    return time.monotonic()


def _env_bool(name: str, default: bool = False) -> bool:
    """Read a boolean env var. Truthy: 1, true, yes, on (case-insensitive)."""
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    """Read an integer env var, returning default on missing or invalid value."""
    v = os.getenv(name)
    if v is None:
        return default
    try:
        return int(v.strip())
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    """Read a float env var, returning default on missing or invalid value."""
    v = os.getenv(name)
    if v is None:
        return default
    try:
        return float(v.strip())
    except Exception:
        return default
