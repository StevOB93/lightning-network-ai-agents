"""Tests for ai.command_queue — file-based inbox/outbox message bus.

Covers:
  - QueuePaths construction (paths(), with/without agent_id)
  - ensure() creates directory tree and files
  - _next_id() increments atomically
  - enqueue() writes valid JSONL to inbox
  - read_new() byte-offset cursor tracking
  - read_new() self-healing on inbox truncation
  - read_new() skips malformed lines
  - read_new() leaves partial (incomplete) writes unread
  - write_outbox() appends to outbox
  - last_outbox() reads most recent entry
  - last_outbox() handles empty/malformed outbox

All tests use tmp_path + monkeypatch to isolate from real runtime/.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai.command_queue import (
    QueuePaths,
    _next_id,
    ensure,
    enqueue,
    last_outbox,
    paths,
    read_new,
    write_outbox,
)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture()
def queue_root(tmp_path, monkeypatch):
    """Redirect _repo_root() to tmp_path so all files land in a temp directory."""
    monkeypatch.setattr("ai.command_queue._repo_root", lambda: tmp_path)
    return tmp_path


# =============================================================================
# paths()
# =============================================================================

class TestPaths:
    def test_default_paths(self):
        qp = paths()
        assert qp.base_dir.name == "agent"
        assert qp.inbox.name == "inbox.jsonl"
        assert qp.outbox.name == "outbox.jsonl"
        assert qp.offset.name == "inbox.offset"
        assert qp.counter.name == "msg.counter"

    def test_agent_id_paths(self):
        qp = paths(agent_id="2")
        assert qp.base_dir.name == "agent-2"
        assert qp.inbox.parent.name == "agent-2"

    def test_none_agent_id_same_as_default(self):
        assert paths(None).base_dir == paths().base_dir

    def test_different_agents_are_isolated(self):
        assert paths(agent_id="1").base_dir != paths(agent_id="2").base_dir


# =============================================================================
# ensure()
# =============================================================================

class TestEnsure:
    def test_creates_directory(self, queue_root):
        qp = ensure()
        assert qp.base_dir.exists()
        assert qp.inbox.exists()
        assert qp.outbox.exists()
        assert qp.offset.exists()
        assert qp.counter.exists()

    def test_offset_initialized_to_zero(self, queue_root):
        qp = ensure()
        assert qp.offset.read_text().strip() == "0"

    def test_counter_initialized_to_zero(self, queue_root):
        qp = ensure()
        assert qp.counter.read_text().strip() == "0"

    def test_idempotent(self, queue_root):
        ensure()
        ensure()  # Should not raise

    def test_agent_id_creates_agent_dir(self, queue_root):
        qp = ensure(agent_id="5")
        assert qp.base_dir.name == "agent-5"
        assert qp.base_dir.exists()


# =============================================================================
# _next_id()
# =============================================================================

class TestNextId:
    def test_starts_at_one(self, queue_root):
        assert _next_id() == 1

    def test_increments(self, queue_root):
        assert _next_id() == 1
        assert _next_id() == 2
        assert _next_id() == 3

    def test_per_agent_counters_independent(self, queue_root):
        assert _next_id(agent_id="1") == 1
        assert _next_id(agent_id="2") == 1
        assert _next_id(agent_id="1") == 2

    def test_survives_corrupted_counter(self, queue_root):
        qp = ensure()
        qp.counter.write_text("not_a_number")
        # Should reset to 0 and return 1
        assert _next_id() == 1


# =============================================================================
# enqueue()
# =============================================================================

class TestEnqueue:
    def test_returns_message_with_id(self, queue_root):
        msg = enqueue("hello")
        assert msg["id"] == 1
        assert msg["content"] == "hello"
        assert msg["role"] == "user"
        assert "ts" in msg

    def test_writes_to_inbox(self, queue_root):
        enqueue("test message")
        qp = paths()
        line = qp.inbox.read_text().strip()
        obj = json.loads(line)
        assert obj["content"] == "test message"

    def test_meta_passthrough(self, queue_root):
        msg = enqueue("cmd", meta={"kind": "health_check"})
        assert msg["meta"]["kind"] == "health_check"

    def test_default_meta_is_empty_dict(self, queue_root):
        msg = enqueue("cmd")
        assert msg["meta"] == {}

    def test_multiple_enqueues_append(self, queue_root):
        enqueue("first")
        enqueue("second")
        qp = paths()
        lines = [l for l in qp.inbox.read_text().splitlines() if l.strip()]
        assert len(lines) == 2
        assert json.loads(lines[0])["content"] == "first"
        assert json.loads(lines[1])["content"] == "second"

    def test_enqueue_to_specific_agent(self, queue_root):
        enqueue("for agent 3", agent_id="3")
        qp = paths(agent_id="3")
        obj = json.loads(qp.inbox.read_text().strip())
        assert obj["content"] == "for agent 3"

    def test_ids_increment_across_enqueues(self, queue_root):
        m1 = enqueue("a")
        m2 = enqueue("b")
        assert m2["id"] == m1["id"] + 1


# =============================================================================
# read_new()
# =============================================================================

class TestReadNew:
    def test_empty_inbox_returns_empty(self, queue_root):
        ensure()
        assert read_new() == []

    def test_reads_enqueued_messages(self, queue_root):
        enqueue("hello")
        msgs = read_new()
        assert len(msgs) == 1
        assert msgs[0]["content"] == "hello"

    def test_cursor_advances(self, queue_root):
        enqueue("first")
        msgs1 = read_new()
        assert len(msgs1) == 1
        # Second read should return nothing (cursor advanced past first msg)
        msgs2 = read_new()
        assert len(msgs2) == 0

    def test_new_messages_after_cursor(self, queue_root):
        enqueue("first")
        read_new()
        enqueue("second")
        msgs = read_new()
        assert len(msgs) == 1
        assert msgs[0]["content"] == "second"

    def test_skips_malformed_lines(self, queue_root):
        qp = ensure()
        qp.inbox.write_text(
            '{"id":1,"content":"good","role":"user","meta":{},"ts":1}\n'
            'NOT JSON\n'
            '{"id":2,"content":"also good","role":"user","meta":{},"ts":2}\n'
        )
        msgs = read_new()
        assert len(msgs) == 2

    def test_self_heals_on_truncation(self, queue_root):
        enqueue("before truncation")
        read_new()  # advances cursor
        # Truncate the inbox (simulate user clearing it)
        qp = paths()
        qp.inbox.write_text("")
        # Write new message
        enqueue("after truncation")
        msgs = read_new()
        assert len(msgs) == 1
        assert msgs[0]["content"] == "after truncation"

    def test_partial_line_not_consumed(self, queue_root):
        """A line without trailing newline is left for the next read."""
        qp = ensure()
        # Write a complete line + a partial line (no newline at end)
        qp.inbox.write_text(
            '{"id":1,"content":"complete","role":"user","meta":{},"ts":1}\n'
            '{"id":2,"content":"partial","role":"user","meta":{},"ts":2}'
        )
        msgs = read_new()
        assert len(msgs) == 1
        assert msgs[0]["content"] == "complete"

    def test_per_agent_read_isolation(self, queue_root):
        enqueue("for agent 1", agent_id="1")
        enqueue("for agent 2", agent_id="2")
        msgs_1 = read_new(agent_id="1")
        msgs_2 = read_new(agent_id="2")
        assert len(msgs_1) == 1
        assert msgs_1[0]["content"] == "for agent 1"
        assert len(msgs_2) == 1
        assert msgs_2[0]["content"] == "for agent 2"


# =============================================================================
# write_outbox()
# =============================================================================

class TestWriteOutbox:
    def test_writes_to_outbox(self, queue_root):
        ensure()
        write_outbox({"result": "ok", "data": 42})
        qp = paths()
        obj = json.loads(qp.outbox.read_text().strip())
        assert obj["result"] == "ok"
        assert obj["data"] == 42

    def test_appends_multiple(self, queue_root):
        ensure()
        write_outbox({"i": 1})
        write_outbox({"i": 2})
        qp = paths()
        lines = [l for l in qp.outbox.read_text().splitlines() if l.strip()]
        assert len(lines) == 2

    def test_per_agent_outbox(self, queue_root):
        ensure(agent_id="7")
        write_outbox({"result": "agent7"}, agent_id="7")
        qp = paths(agent_id="7")
        obj = json.loads(qp.outbox.read_text().strip())
        assert obj["result"] == "agent7"


# =============================================================================
# last_outbox()
# =============================================================================

class TestLastOutbox:
    def test_empty_outbox_returns_none(self, queue_root):
        ensure()
        assert last_outbox() is None

    def test_returns_most_recent(self, queue_root):
        ensure()
        write_outbox({"i": 1})
        write_outbox({"i": 2})
        write_outbox({"i": 3})
        result = last_outbox()
        assert result is not None
        assert result["i"] == 3

    def test_skips_malformed_trailing_lines(self, queue_root):
        qp = ensure()
        # Write good entry then a malformed line
        qp.outbox.write_text(
            '{"i": 1}\n'
            'NOT JSON\n'
        )
        result = last_outbox()
        assert result is not None
        assert result["i"] == 1

    def test_per_agent_last_outbox(self, queue_root):
        ensure(agent_id="4")
        write_outbox({"val": "first"}, agent_id="4")
        write_outbox({"val": "second"}, agent_id="4")
        result = last_outbox(agent_id="4")
        assert result is not None
        assert result["val"] == "second"

    def test_agents_dont_cross_read(self, queue_root):
        ensure(agent_id="a")
        ensure(agent_id="b")
        write_outbox({"from": "a"}, agent_id="a")
        assert last_outbox(agent_id="b") is None
