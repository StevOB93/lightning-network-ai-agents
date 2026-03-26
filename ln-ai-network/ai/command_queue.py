from __future__ import annotations

# =============================================================================
# command_queue — file-based inbox/outbox message bus
#
# The agent and pipeline communicate with the outside world (UI, CLI) through
# two append-only JSONL files:
#
#   inbox.jsonl   — commands written by the UI/CLI, consumed by the agent
#   outbox.jsonl  — responses written by the agent, consumed by the UI
#
# All files live under runtime/agent/:
#   inbox.jsonl   — messages waiting for the agent
#   outbox.jsonl  — responses from the agent
#   inbox.offset  — byte offset into inbox.jsonl; the agent's read cursor
#   msg.counter   — monotonically increasing message ID counter
#
# Key design decisions:
#   - Byte-offset reading (not line counting): the agent records how many bytes
#     it has consumed; on restart it seeks to that offset and reads only new data.
#     This survives restarts, blank lines, and partial writes gracefully.
#   - fcntl.LOCK_EX for concurrent write safety: the counter and inbox files are
#     locked during write to prevent partial writes from concurrent processes.
#     Falls back to no-op on Windows (no fcntl).
#   - Self-healing: if the inbox is truncated (e.g. manually cleared) while the
#     offset file still points past EOF, read_new() resets the offset to 0 so
#     the agent can read messages that were re-enqueued.
#   - last_outbox() reads the last 8KB of outbox.jsonl rather than the whole
#     file — keeps the response fast even when outbox grows large over time.
# =============================================================================

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import fcntl  # Linux/WSL file locking
except Exception:
    fcntl = None  # type: ignore


@dataclass(frozen=True)
class QueuePaths:
    """All file paths for the message queue, resolved once and stored together."""
    base_dir: Path
    inbox: Path
    outbox: Path
    offset: Path    # Byte position up to which inbox.jsonl has been consumed
    counter: Path   # Monotonically increasing message ID counter


def _repo_root() -> Path:
    # ai/command_queue.py → ai/ → repo root (ln-ai-network/)
    return Path(__file__).resolve().parents[1]


def paths() -> QueuePaths:
    """Return all queue file paths relative to the repo root."""
    root = _repo_root()
    base = root / "runtime" / "agent"
    return QueuePaths(
        base_dir=base,
        inbox=base / "inbox.jsonl",
        outbox=base / "outbox.jsonl",
        offset=base / "inbox.offset",
        counter=base / "msg.counter",
    )


# ── File locking helpers ────────────────────────────────────────────────────

def _lock(f) -> None:
    """Acquire an exclusive blocking lock on the file (no-op on Windows)."""
    if fcntl is None:
        return
    fcntl.flock(f.fileno(), fcntl.LOCK_EX)


def _unlock(f) -> None:
    """Release the exclusive lock (no-op on Windows)."""
    if fcntl is None:
        return
    fcntl.flock(f.fileno(), fcntl.LOCK_UN)


# ── Queue initialization ─────────────────────────────────────────────────────

def ensure() -> QueuePaths:
    """
    Create the queue directory and all queue files if they don't exist.

    Called at the start of every read/write operation so callers never
    need to pre-initialize — the queue self-bootstraps on first use.
    """
    qp = paths()
    qp.base_dir.mkdir(parents=True, exist_ok=True)
    qp.inbox.touch(exist_ok=True)
    qp.outbox.touch(exist_ok=True)
    if not qp.offset.exists():
        qp.offset.write_text("0", encoding="utf-8")
    if not qp.counter.exists():
        qp.counter.write_text("0", encoding="utf-8")
    return qp


# ── Message ID counter ───────────────────────────────────────────────────────

def _next_id() -> int:
    """
    Atomically increment and return the next message ID.

    Uses LOCK_EX + truncate + write to ensure the read-modify-write is atomic
    even when the UI and agent run concurrently. The counter file always contains
    a single integer (the last assigned ID).
    """
    qp = ensure()
    with qp.counter.open("r+", encoding="utf-8") as f:
        _lock(f)
        try:
            raw = f.read().strip() or "0"
            try:
                n = int(raw)
            except Exception:
                n = 0
            n += 1
            f.seek(0)
            f.truncate()
            f.write(str(n))
            f.flush()
            os.fsync(f.fileno())  # Durable before unlock
            return n
        finally:
            # Always release the lock, even if fsync() or write() raises (e.g.
            # disk full). The 'with' block closing the file handle would also
            # release the flock, but doing it explicitly here minimises the
            # window between exception and release.
            _unlock(f)


# ── Public API ───────────────────────────────────────────────────────────────

def enqueue(content: str, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Write a new message to inbox.jsonl.

    The message is a JSON object with fields:
      id      — monotonically increasing, unique per queue
      ts      — Unix timestamp (seconds)
      role    — always "user" (messages come from the user/UI side)
      content — the raw prompt or command text
      meta    — dict with routing hints (kind, use_llm, etc.) for the agent

    Returns the full message dict so the caller can log the assigned ID.
    """
    qp = ensure()
    msg = {
        "id": _next_id(),
        "ts": int(time.time()),
        "role": "user",
        "content": content,
        "meta": meta or {},
    }

    line = json.dumps(msg, ensure_ascii=False) + "\n"
    with qp.inbox.open("a", encoding="utf-8") as f:
        _lock(f)
        try:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())  # Force to disk before releasing lock
        finally:
            _unlock(f)

    return msg


def read_new() -> List[Dict[str, Any]]:
    """
    Read and return all inbox messages that have arrived since the last call.

    Uses a byte-offset cursor (inbox.offset) rather than line numbers so the
    agent survives restarts without re-processing old messages.

    Self-healing: if inbox.jsonl was truncated externally (e.g. cleared via the
    UI) while the offset file still pointed past EOF, the offset is reset to 0
    so the agent re-reads from the beginning. This is safe because a truncated
    inbox means the old messages are gone — re-reading from 0 will only find
    whatever new messages were written after the truncation.

    Malformed lines are silently skipped so a single bad entry doesn't block
    all subsequent messages.
    """
    qp = ensure()
    try:
        offset = int(qp.offset.read_text(encoding="utf-8").strip() or "0")
    except Exception:
        offset = 0

    # Self-heal: inbox truncated while offset persisted past the new EOF
    try:
        inbox_size = qp.inbox.stat().st_size
    except Exception:
        inbox_size = 0

    if offset > inbox_size:
        offset = 0
        qp.offset.write_text("0", encoding="utf-8")

    # Read all bytes from the current offset to EOF
    with qp.inbox.open("rb") as f:
        f.seek(offset)
        data = f.read()

    if not data:
        return []

    msgs: List[Dict[str, Any]] = []
    consumed_bytes = 0
    for ln in data.splitlines(keepends=True):
        # Only advance past lines that end with a newline (complete writes).
        # A partial line at EOF (no trailing newline) means a concurrent write
        # is in progress — leave it for the next read.
        if not ln.endswith(b"\n"):
            break
        consumed_bytes += len(ln)
        try:
            obj = json.loads(ln.decode("utf-8"))
            if isinstance(obj, dict):
                msgs.append(obj)
        except Exception:
            continue  # Skip malformed lines deterministically

    # Advance the cursor only past fully consumed (newline-terminated) lines
    if consumed_bytes > 0:
        qp.offset.write_text(str(offset + consumed_bytes), encoding="utf-8")
    return msgs


def write_outbox(entry: Dict[str, Any]) -> None:
    """
    Append a response entry to outbox.jsonl.

    Called by the agent/pipeline to publish results. The UI server polls
    this file's mtime every 400ms and pushes an SSE event when it changes.

    Uses LOCK_EX + fsync for the same durability guarantees as enqueue().
    """
    qp = ensure()
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with qp.outbox.open("a", encoding="utf-8") as f:
        _lock(f)
        try:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        finally:
            _unlock(f)


def last_outbox() -> Optional[Dict[str, Any]]:
    """
    Return the most recent valid entry from outbox.jsonl without loading the
    entire file.

    Reads the last 8KB from the end of the file (using SEEK_END), then scans
    backward through lines until it finds a parseable JSON object. This keeps
    the response latency constant regardless of outbox size.

    Returns None if the outbox is empty or all trailing lines are malformed.
    """
    qp = ensure()
    with qp.outbox.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        if size == 0:
            return None
        # Read the last 8KB (or the whole file if it's smaller)
        back = min(8192, size)
        f.seek(-back, os.SEEK_END)
        chunk = f.read().decode("utf-8", errors="ignore")

    lines = [x for x in chunk.splitlines() if x.strip()]
    # Scan backwards: the most recent entry is at the bottom
    for ln in reversed(lines):
        try:
            obj = json.loads(ln)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None
