from __future__ import annotations

# =============================================================================
# AgentRegistry — lightweight process discovery and inter-agent routing
#
# Each running process (pipeline, agent) registers itself at startup by
# appending a JSON record to runtime/registry.jsonl. The record contains
# the process kind ("pipeline" | "agent"), node number, PID, and the path
# to its inbox file.
#
# On read, stale entries (PIDs that are no longer running) are automatically
# filtered out, so the live registry always reflects currently-running peers.
#
# Routing:
#   route_to(kind, node, message) writes a message dict to the target
#   process's inbox. The pipeline's run() loop handles "route" kind messages
#   by calling this method, allowing one pipeline to delegate tasks to
#   another node's pipeline or agent.
#
# File format (runtime/registry.jsonl):
#   Each line: {"kind": "pipeline", "node": 1, "pid": 1234,
#               "inbox": "runtime/agent/inbox.jsonl", "ts": 1700000000}
#
# Thread/process safety:
#   Writes use 'a' (append) mode, which is atomic on Linux for lines < PIPE_BUF
#   (4 KB). Reads are best-effort — a stale entry is harmless (filtered by PID
#   check). No locking is needed for the typical single-writer-per-PID pattern.
# =============================================================================

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


class AgentRegistry:
    """
    Lightweight peer-discovery registry backed by a JSONL file.

    Usage:
      registry = AgentRegistry(registry_path)
      registry.register("pipeline", node=1, inbox_path=inbox)   # on startup
      peers = registry.list_peers()                              # discover others
      registry.route_to("pipeline", node=2, message={...})      # send a task
    """

    def __init__(self, registry_path: Path) -> None:
        self.path = registry_path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ── Registration ────────────────────────────────────────────────────────

    def register(self, kind: str, node: int, inbox_path: Path) -> None:
        """
        Write a registration record for this process.

        Called once at startup. Appends to the shared registry file so
        existing entries from other processes are preserved.
        """
        record = {
            "kind":  kind,
            "node":  node,
            "pid":   os.getpid(),
            "inbox": str(inbox_path),
            "ts":    int(time.time()),
        }
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass  # Registry is advisory — failure must not prevent startup

    # ── Discovery ────────────────────────────────────────────────────────────

    def list_peers(self) -> List[Dict[str, Any]]:
        """
        Return all currently-running registered processes.

        Reads the full registry file and filters out stale entries by checking
        whether each PID is still alive (os.kill(pid, 0) — no signal sent,
        just existence check). Our own PID is included in the list.
        """
        if not self.path.exists():
            return []
        peers = []
        seen_pids: set = set()
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return []
        for line in reversed(lines):  # most-recent entry wins for duplicate PIDs
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            pid = record.get("pid")
            if pid in seen_pids:
                continue
            seen_pids.add(pid)
            if _pid_alive(pid):
                peers.append(record)
        return peers

    def find_peer(self, kind: str, node: int) -> Optional[Dict[str, Any]]:
        """
        Return the registration record for the given kind+node, or None.

        If multiple matching records exist (should not happen in normal use),
        returns the one with the most recent timestamp.
        """
        matches = [
            p for p in self.list_peers()
            if p.get("kind") == kind and p.get("node") == node
        ]
        if not matches:
            return None
        return max(matches, key=lambda p: p.get("ts", 0))

    # ── Routing ───────────────────────────────────────────────────────────────

    def route_to(self, kind: str, node: int, message: Dict[str, Any]) -> bool:
        """
        Deliver a message to the inbox of the target kind+node process.

        Finds the target's inbox path via find_peer(), then appends the
        message as a JSONL record. Returns True on success, False if the
        target is not found or the write fails.

        The message dict should follow the same shape as normal inbox
        messages: {"id": N, "content": "...", "meta": {"kind": "freeform", ...}}.
        """
        peer = self.find_peer(kind, node)
        if peer is None:
            return False
        inbox_path = Path(peer["inbox"])
        if not inbox_path.parent.exists():
            return False
        try:
            record = dict(message)
            record.setdefault("routed_from_pid", os.getpid())
            record.setdefault("routed_ts", int(time.time()))
            with inbox_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            return True
        except Exception:
            return False

    # ── Cleanup ────────────────────────────────────────────────────────────────

    def purge_stale(self) -> int:
        """
        Rewrite the registry file with only live-process entries.

        Returns the number of stale records removed. Called opportunistically
        at startup to prevent the file from growing unboundedly over many
        restarts.
        """
        if not self.path.exists():
            return 0
        live = self.list_peers()
        live_pids = {p["pid"] for p in live}
        removed = 0
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
            kept = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    if record.get("pid") in live_pids:
                        kept.append(line)
                    else:
                        removed += 1
                except json.JSONDecodeError:
                    removed += 1
            self.path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        except Exception:
            pass
        return removed


# =============================================================================
# Helper
# =============================================================================

def _pid_alive(pid: Any) -> bool:
    """Return True if the given PID is a currently-running process."""
    try:
        os.kill(int(pid), 0)  # signal 0 = existence check, no signal sent
        return True
    except (OSError, TypeError, ValueError):
        return False
