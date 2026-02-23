from __future__ import annotations

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
    base_dir: Path
    inbox: Path
    outbox: Path
    offset: Path
    counter: Path


def _repo_root() -> Path:
    # ai/command_queue.py -> ai -> repo root
    return Path(__file__).resolve().parents[1]


def paths() -> QueuePaths:
    root = _repo_root()
    base = root / "runtime" / "agent"
    return QueuePaths(
        base_dir=base,
        inbox=base / "inbox.jsonl",
        outbox=base / "outbox.jsonl",
        offset=base / "inbox.offset",
        counter=base / "msg.counter",
    )


def _lock(f) -> None:
    if fcntl is None:
        return
    fcntl.flock(f.fileno(), fcntl.LOCK_EX)


def _unlock(f) -> None:
    if fcntl is None:
        return
    fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def ensure() -> QueuePaths:
    qp = paths()
    qp.base_dir.mkdir(parents=True, exist_ok=True)
    qp.inbox.touch(exist_ok=True)
    qp.outbox.touch(exist_ok=True)
    if not qp.offset.exists():
        qp.offset.write_text("0", encoding="utf-8")
    if not qp.counter.exists():
        qp.counter.write_text("0", encoding="utf-8")
    return qp


def _next_id() -> int:
    qp = ensure()
    with qp.counter.open("r+", encoding="utf-8") as f:
        _lock(f)
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
        os.fsync(f.fileno())
        _unlock(f)
        return n


def enqueue(content: str, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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
        f.write(line)
        f.flush()
        os.fsync(f.fileno())
        _unlock(f)

    return msg


def read_new() -> List[Dict[str, Any]]:
    """
    Agent-side: read new inbox entries since last offset (byte offset).
    Deterministic across restarts.
    """
    qp = ensure()
    try:
        offset = int(qp.offset.read_text(encoding="utf-8").strip() or "0")
    except Exception:
        offset = 0

    with qp.inbox.open("rb") as f:
        f.seek(offset)
        data = f.read()

    if not data:
        return []

    msgs: List[Dict[str, Any]] = []
    for ln in data.splitlines():
        try:
            obj = json.loads(ln.decode("utf-8"))
            if isinstance(obj, dict):
                msgs.append(obj)
        except Exception:
            # Ignore malformed lines deterministically
            continue

    qp.offset.write_text(str(offset + len(data)), encoding="utf-8")
    return msgs


def write_outbox(entry: Dict[str, Any]) -> None:
    qp = ensure()
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with qp.outbox.open("a", encoding="utf-8") as f:
        _lock(f)
        f.write(line)
        f.flush()
        os.fsync(f.fileno())
        _unlock(f)


def last_outbox() -> Optional[Dict[str, Any]]:
    qp = ensure()
    # Read last ~8KB and parse from bottom
    with qp.outbox.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        if size == 0:
            return None
        back = min(8192, size)
        f.seek(-back, os.SEEK_END)
        chunk = f.read().decode("utf-8", errors="ignore")

    lines = [x for x in chunk.splitlines() if x.strip()]
    for ln in reversed(lines):
        try:
            obj = json.loads(ln)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None