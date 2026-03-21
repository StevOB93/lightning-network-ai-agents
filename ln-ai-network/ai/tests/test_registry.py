"""Tests for ai.core.registry.AgentRegistry.

Strategy:
  - All tests use a temporary directory so no runtime/ state is touched.
  - PID existence checks use the real os.kill(pid, 0) — the current process's
    own PID is always alive; a fake high PID is assumed dead.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ai.core.registry import AgentRegistry, _pid_alive


# =============================================================================
# Helpers
# =============================================================================

DEAD_PID = 99_999_999  # Assumed not running; adjust if this PID is ever live


@pytest.fixture
def reg(tmp_path):
    return AgentRegistry(tmp_path / "registry.jsonl")


# =============================================================================
# _pid_alive helper
# =============================================================================

def test_pid_alive_current_process():
    """The current process's PID is always alive."""
    assert _pid_alive(os.getpid()) is True


def test_pid_alive_dead_pid():
    """A very high fake PID should not be alive."""
    assert _pid_alive(DEAD_PID) is False


def test_pid_alive_invalid_input():
    """Non-integer input returns False without raising."""
    assert _pid_alive("not_a_pid") is False
    assert _pid_alive(None) is False


# =============================================================================
# register + list_peers
# =============================================================================

def test_register_creates_file(reg, tmp_path):
    reg.register("pipeline", node=1, inbox_path=tmp_path / "inbox.jsonl")
    assert reg.path.exists()


def test_register_record_fields(reg, tmp_path):
    reg.register("agent", node=2, inbox_path=tmp_path / "inbox.jsonl")
    line = reg.path.read_text().strip()
    record = json.loads(line)
    assert record["kind"] == "agent"
    assert record["node"] == 2
    assert record["pid"] == os.getpid()
    assert "inbox" in record
    assert "ts" in record


def test_list_peers_includes_current_process(reg, tmp_path):
    reg.register("pipeline", node=1, inbox_path=tmp_path / "inbox.jsonl")
    peers = reg.list_peers()
    assert any(p["pid"] == os.getpid() for p in peers)


def test_list_peers_excludes_dead_pids(reg, tmp_path):
    # Manually write a stale record with a dead PID
    stale = {"kind": "pipeline", "node": 9, "pid": DEAD_PID,
              "inbox": str(tmp_path / "x.jsonl"), "ts": 0}
    reg.path.write_text(json.dumps(stale) + "\n")
    peers = reg.list_peers()
    assert not any(p["pid"] == DEAD_PID for p in peers)


def test_list_peers_empty_when_no_file(reg):
    assert reg.list_peers() == []


# =============================================================================
# find_peer
# =============================================================================

def test_find_peer_returns_matching_record(reg, tmp_path):
    reg.register("pipeline", node=3, inbox_path=tmp_path / "inbox.jsonl")
    peer = reg.find_peer("pipeline", node=3)
    assert peer is not None
    assert peer["node"] == 3


def test_find_peer_returns_none_for_missing(reg):
    assert reg.find_peer("pipeline", node=99) is None


def test_find_peer_returns_none_for_wrong_kind(reg, tmp_path):
    reg.register("agent", node=1, inbox_path=tmp_path / "inbox.jsonl")
    assert reg.find_peer("pipeline", node=1) is None


# =============================================================================
# route_to
# =============================================================================

def test_route_to_writes_to_target_inbox(reg, tmp_path):
    inbox = tmp_path / "target_inbox.jsonl"
    reg.register("pipeline", node=5, inbox_path=inbox)
    msg = {"id": 1, "content": "hello", "meta": {"kind": "freeform"}}
    ok = reg.route_to("pipeline", node=5, message=msg)
    assert ok is True
    assert inbox.exists()
    record = json.loads(inbox.read_text().strip())
    assert record["content"] == "hello"
    assert "routed_from_pid" in record


def test_route_to_returns_false_when_no_peer(reg):
    ok = reg.route_to("pipeline", node=42, message={"content": "hi"})
    assert ok is False


# =============================================================================
# purge_stale
# =============================================================================

def test_purge_stale_removes_dead_entries(reg, tmp_path):
    # Write one live (our own PID) and one dead entry
    live = {"kind": "pipeline", "node": 1, "pid": os.getpid(),
            "inbox": str(tmp_path / "a.jsonl"), "ts": 0}
    dead = {"kind": "pipeline", "node": 2, "pid": DEAD_PID,
            "inbox": str(tmp_path / "b.jsonl"), "ts": 0}
    with reg.path.open("w") as f:
        f.write(json.dumps(live) + "\n" + json.dumps(dead) + "\n")

    removed = reg.purge_stale()
    assert removed == 1

    remaining = [json.loads(l) for l in reg.path.read_text().splitlines() if l.strip()]
    pids = [r["pid"] for r in remaining]
    assert os.getpid() in pids
    assert DEAD_PID not in pids


def test_purge_stale_no_file_returns_zero(reg):
    assert reg.purge_stale() == 0


# =============================================================================
# await_reply
# =============================================================================

def test_await_reply_returns_matching_message(reg, tmp_path):
    """await_reply returns the first message with in_reply_to == reply_id."""
    inbox = tmp_path / "inbox.jsonl"
    reply_id = "test-reply-123"
    # Write a matching reply into the inbox before calling await_reply
    reply = {"in_reply_to": reply_id, "content": "done"}
    inbox.write_text(json.dumps(reply) + "\n")
    result = reg.await_reply(reply_id, inbox, timeout_s=0.5)
    assert result is not None
    assert result["content"] == "done"


def test_await_reply_returns_none_on_timeout(reg, tmp_path):
    """await_reply returns None when no matching message arrives before timeout."""
    inbox = tmp_path / "inbox.jsonl"
    result = reg.await_reply("no-such-id", inbox, timeout_s=0.2, poll_interval_s=0.05)
    assert result is None


def test_await_reply_skips_non_matching_messages(reg, tmp_path):
    """await_reply ignores messages with different in_reply_to values."""
    inbox = tmp_path / "inbox.jsonl"
    other = {"in_reply_to": "different-id", "content": "wrong"}
    target = {"in_reply_to": "correct-id", "content": "right"}
    inbox.write_text(json.dumps(other) + "\n" + json.dumps(target) + "\n")
    result = reg.await_reply("correct-id", inbox, timeout_s=0.5)
    assert result is not None
    assert result["content"] == "right"
