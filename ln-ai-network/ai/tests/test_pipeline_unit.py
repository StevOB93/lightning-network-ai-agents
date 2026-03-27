"""Unit tests for ai.pipeline — PipelineCoordinator internals.

Covers:
  - _load_history() validation (role, content), compaction, and corrupt-line handling
  - _update_history() deduplication and archive writes
  - _verify_goal() tool mapping
  - _VERIFY_TOOL intent → tool name lookup
  - PipelineResult.to_outbox_dict() wire format
  - IntentBlock.to_dict() serialization
  - _send_route_reply() message format

Strategy:
  - History tests use a minimal harness that instantiates just the history
    methods on a fake object (not the full PipelineCoordinator which needs
    MCP, LLM, etc.).
  - No live infrastructure or API calls required.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

import time

from ai.core.config import AgentConfig
from ai.models import IntentBlock, PipelineResult
from ai.pipeline import PIPELINE_BUILD, PipelineCoordinator, _VERIFY_TOOL, _friendly_error
from ai.utils import TraceLogger


# =============================================================================
# Harness: isolates _load_history / _update_history from full coordinator
# =============================================================================

class _HistoryHarness:
    """Mimics the exact _load_history / _update_history logic from PipelineCoordinator."""

    def __init__(self, history_path: Path, archive_path: Path, cfg: AgentConfig) -> None:
        self._history_path = history_path
        self._archive_path = archive_path
        self._cfg = cfg
        # Call PipelineCoordinator._load_history on self (duck-typed)
        self._history: List[Dict[str, Any]] = PipelineCoordinator._load_history(self)

    def update(self, user_text: str, assistant_summary: str,
               outcome: str = "ok", human_summary: str = "") -> None:
        PipelineCoordinator._update_history(self, user_text, assistant_summary, outcome, human_summary)


@pytest.fixture()
def cfg():
    return AgentConfig(max_history_messages=3)  # 3 pairs = 6 messages max


@pytest.fixture()
def history_path(tmp_path):
    return tmp_path / "history.jsonl"


@pytest.fixture()
def archive_path(tmp_path):
    return tmp_path / "archive.jsonl"


# =============================================================================
# _load_history: validation
# =============================================================================

class TestLoadHistoryValidation:
    def test_valid_messages_loaded(self, history_path, archive_path, cfg):
        history_path.write_text(
            '{"role": "user", "content": "hello"}\n'
            '{"role": "assistant", "content": "hi there"}\n'
        )
        h = _HistoryHarness(history_path, archive_path, cfg)
        assert len(h._history) == 2

    def test_invalid_role_dropped(self, history_path, archive_path, cfg):
        history_path.write_text(
            '{"role": "system", "content": "injected"}\n'
            '{"role": "user", "content": "valid"}\n'
        )
        h = _HistoryHarness(history_path, archive_path, cfg)
        assert len(h._history) == 1
        assert h._history[0]["role"] == "user"

    def test_empty_content_dropped(self, history_path, archive_path, cfg):
        history_path.write_text(
            '{"role": "user", "content": ""}\n'
            '{"role": "user", "content": "  "}\n'
            '{"role": "user", "content": "valid"}\n'
        )
        h = _HistoryHarness(history_path, archive_path, cfg)
        assert len(h._history) == 1

    def test_non_string_content_dropped(self, history_path, archive_path, cfg):
        history_path.write_text(
            '{"role": "user", "content": 42}\n'
            '{"role": "user", "content": "valid"}\n'
        )
        h = _HistoryHarness(history_path, archive_path, cfg)
        assert len(h._history) == 1

    def test_missing_role_dropped(self, history_path, archive_path, cfg):
        history_path.write_text(
            '{"content": "no role"}\n'
            '{"role": "user", "content": "ok"}\n'
        )
        h = _HistoryHarness(history_path, archive_path, cfg)
        assert len(h._history) == 1

    def test_malformed_json_skipped(self, history_path, archive_path, cfg):
        history_path.write_text(
            '{"role": "user", "content": "before"}\n'
            'NOT JSON\n'
            '{"role": "assistant", "content": "after"}\n'
        )
        h = _HistoryHarness(history_path, archive_path, cfg)
        assert len(h._history) == 2

    def test_missing_file_returns_empty(self, history_path, archive_path, cfg):
        h = _HistoryHarness(history_path, archive_path, cfg)
        assert h._history == []

    def test_empty_file_returns_empty(self, history_path, archive_path, cfg):
        history_path.write_text("")
        h = _HistoryHarness(history_path, archive_path, cfg)
        assert h._history == []


# =============================================================================
# _load_history: compaction
# =============================================================================

class TestLoadHistoryCompaction:
    def test_trims_to_max_on_load(self, history_path, archive_path, cfg):
        """History with 8 messages (4 pairs) gets trimmed to 6 (cfg max 3 pairs)."""
        for i in range(4):
            with history_path.open("a") as f:
                f.write(json.dumps({"role": "user", "content": f"q{i}"}) + "\n")
                f.write(json.dumps({"role": "assistant", "content": f"a{i}"}) + "\n")
        h = _HistoryHarness(history_path, archive_path, cfg)
        assert len(h._history) == 6

    def test_compaction_rewrites_file(self, history_path, archive_path, cfg):
        """After loading >max messages, the file should be compacted to max."""
        for i in range(5):  # 10 messages, max=6
            with history_path.open("a") as f:
                f.write(json.dumps({"role": "user", "content": f"q{i}"}) + "\n")
                f.write(json.dumps({"role": "assistant", "content": f"a{i}"}) + "\n")
        _HistoryHarness(history_path, archive_path, cfg)
        # After compaction, file should only have 6 lines
        lines = [l for l in history_path.read_text().splitlines() if l.strip()]
        assert len(lines) == 6

    def test_keeps_most_recent_after_compaction(self, history_path, archive_path, cfg):
        for i in range(5):
            with history_path.open("a") as f:
                f.write(json.dumps({"role": "user", "content": f"q{i}"}) + "\n")
                f.write(json.dumps({"role": "assistant", "content": f"a{i}"}) + "\n")
        h = _HistoryHarness(history_path, archive_path, cfg)
        # Should keep q2,a2,q3,a3,q4,a4 (last 3 pairs)
        assert h._history[0]["content"] == "q2"
        assert h._history[-1]["content"] == "a4"

    def test_no_compaction_when_under_limit(self, history_path, archive_path, cfg):
        """File with fewer messages than max is not rewritten."""
        history_path.write_text(
            '{"role": "user", "content": "q1"}\n'
            '{"role": "assistant", "content": "a1"}\n'
        )
        original_text = history_path.read_text()
        _HistoryHarness(history_path, archive_path, cfg)
        # File should not be modified
        assert history_path.read_text() == original_text


# =============================================================================
# _update_history: deduplication
# =============================================================================

class TestUpdateHistoryDedup:
    def test_identical_exchange_not_duplicated(self, history_path, archive_path, cfg):
        h = _HistoryHarness(history_path, archive_path, cfg)
        h.update("same question", "same answer")
        h.update("same question", "same answer")
        # Should only be stored once (2 messages, not 4)
        assert len(h._history) == 2

    def test_different_answer_is_stored(self, history_path, archive_path, cfg):
        h = _HistoryHarness(history_path, archive_path, cfg)
        h.update("question", "answer 1")
        h.update("question", "answer 2")
        assert len(h._history) == 4

    def test_different_question_is_stored(self, history_path, archive_path, cfg):
        h = _HistoryHarness(history_path, archive_path, cfg)
        h.update("question 1", "answer")
        h.update("question 2", "answer")
        assert len(h._history) == 4


# =============================================================================
# _update_history: archive writes
# =============================================================================

class TestUpdateHistoryArchive:
    def test_archive_entry_written(self, history_path, archive_path, cfg):
        h = _HistoryHarness(history_path, archive_path, cfg)
        h.update("open channel", "Channel opened", outcome="ok", human_summary="Done")
        assert archive_path.exists()
        record = json.loads(archive_path.read_text().strip())
        assert record["user"] == "open channel"
        assert record["goal"] == "Channel opened"
        assert record["outcome"] == "ok"
        assert record["summary"] == "Done"
        assert "ts" in record

    def test_archive_grows_without_trimming(self, history_path, archive_path, cfg):
        h = _HistoryHarness(history_path, archive_path, cfg)
        for i in range(10):
            h.update(f"q{i}", f"a{i}")
        lines = [l for l in archive_path.read_text().splitlines() if l.strip()]
        assert len(lines) == 10  # All entries preserved

    def test_history_file_trimmed_but_archive_grows(self, history_path, archive_path, cfg):
        h = _HistoryHarness(history_path, archive_path, cfg)
        for i in range(10):
            h.update(f"q{i}", f"a{i}")
        # In-memory history trimmed to max (6 messages = 3 pairs)
        assert len(h._history) == 6
        # Archive has all 10 entries
        archive_lines = [l for l in archive_path.read_text().splitlines() if l.strip()]
        assert len(archive_lines) == 10


# =============================================================================
# _VERIFY_TOOL mapping
# =============================================================================

class TestVerifyToolMapping:
    def test_pay_invoice_maps_to_listfunds(self):
        assert _VERIFY_TOOL["pay_invoice"] == "ln_listfunds"

    def test_open_channel_maps_to_listchannels(self):
        assert _VERIFY_TOOL["open_channel"] == "ln_listchannels"

    def test_rebalance_maps_to_listchannels(self):
        assert _VERIFY_TOOL["rebalance"] == "ln_listchannels"

    def test_set_fee_maps_to_listchannels(self):
        assert _VERIFY_TOOL["set_fee"] == "ln_listchannels"

    def test_noop_not_in_verify(self):
        assert "noop" not in _VERIFY_TOOL

    def test_freeform_not_in_verify(self):
        assert "freeform" not in _VERIFY_TOOL


# =============================================================================
# _verify_goal (via mock)
# =============================================================================

class TestVerifyGoal:
    def _make_intent(self, intent_type: str, context: dict | None = None) -> IntentBlock:
        return IntentBlock(
            goal=f"Test {intent_type}",
            intent_type=intent_type,
            context=context or {},
            success_criteria=[],
            clarifications_needed=[],
            human_summary="Test.",
            raw_prompt="test",
        )

    def test_returns_none_for_noop(self):
        mock = MagicMock()
        result = PipelineCoordinator._verify_goal(mock, self._make_intent("noop"), req_id=1)
        assert result is None

    def test_returns_none_for_freeform(self):
        mock = MagicMock()
        result = PipelineCoordinator._verify_goal(mock, self._make_intent("freeform"), req_id=1)
        assert result is None

    def test_calls_correct_tool_for_pay_invoice(self):
        mock = MagicMock()
        mock.mcp.call.return_value = {"result": {"payload": {"balance": 100}}}
        mock.trace = MagicMock()
        intent = self._make_intent("pay_invoice", {"from_node": 1})
        result = PipelineCoordinator._verify_goal(mock, intent, req_id=1)
        mock.mcp.call.assert_called_once_with("ln_listfunds", {"node": 1})
        assert result is not None
        assert "Verified" in result

    def test_uses_node_from_context(self):
        mock = MagicMock()
        mock.mcp.call.return_value = {"result": {"payload": {"channels": []}}}
        mock.trace = MagicMock()
        intent = self._make_intent("open_channel", {"from_node": 2})
        PipelineCoordinator._verify_goal(mock, intent, req_id=1)
        mock.mcp.call.assert_called_once_with("ln_listchannels", {"node": 2})

    def test_returns_none_on_mcp_failure(self):
        mock = MagicMock()
        mock.mcp.call.side_effect = Exception("MCP timeout")
        mock.trace = MagicMock()
        intent = self._make_intent("pay_invoice", {"from_node": 1})
        result = PipelineCoordinator._verify_goal(mock, intent, req_id=1)
        assert result is None


# =============================================================================
# IntentBlock serialization
# =============================================================================

class TestIntentBlockSerialization:
    def test_to_dict(self):
        intent = IntentBlock(
            goal="Open channel",
            intent_type="open_channel",
            context={"from_node": 1, "to_node": 2, "capacity_sat": 500000},
            success_criteria=["Channel appears in listchannels"],
            clarifications_needed=[],
            human_summary="Opening a 500k sat channel from node 1 to node 2.",
            raw_prompt="open a channel between node 1 and 2 for 500000 sats",
        )
        d = intent.to_dict()
        assert d["goal"] == "Open channel"
        assert d["intent_type"] == "open_channel"
        assert d["context"]["capacity_sat"] == 500000
        assert "raw_prompt" in d


# =============================================================================
# PipelineResult.to_outbox_dict()
# =============================================================================

class TestPipelineResultSerialization:
    def test_success_result(self):
        intent = IntentBlock(
            goal="Check balance", intent_type="freeform",
            context={}, success_criteria=[], clarifications_needed=[],
            human_summary="Checking.", raw_prompt="what is the balance",
        )
        result = PipelineResult(
            request_id=1, ts=1000, success=True,
            stage_failed=None, intent=intent, plan=None,
            step_results=[], human_summary="Balance is 500000 sat.",
            error=None, pipeline_build="test-build",
        )
        d = result.to_outbox_dict()
        assert d["success"] is True
        assert d["content"] == "Balance is 500000 sat."
        assert d["type"] == "pipeline_report"
        assert d["pipeline_build"] == "test-build"
        assert d["stage_failed"] is None
        assert d["intent"]["goal"] == "Check balance"

    def test_failure_result(self):
        result = PipelineResult(
            request_id=2, ts=2000, success=False,
            stage_failed="translator", intent=None, plan=None,
            step_results=[], human_summary="Failed to parse.",
            error="bad JSON", pipeline_build="test-build",
        )
        d = result.to_outbox_dict()
        assert d["success"] is False
        assert d["stage_failed"] == "translator"
        assert d["intent"] is None
        assert d["error"] == "bad JSON"

    def test_content_is_human_summary(self):
        """The outbox 'content' field must alias human_summary for UI compatibility."""
        result = PipelineResult(
            request_id=3, ts=3000, success=True,
            stage_failed=None, intent=None, plan=None,
            step_results=[], human_summary="The answer.",
            error=None, pipeline_build="v1",
        )
        assert result.to_outbox_dict()["content"] == "The answer."


# =============================================================================
# _send_route_reply message format
# =============================================================================

class TestSendRouteReply:
    def test_reply_written_to_inbox(self, tmp_path):
        reply_inbox = tmp_path / "sender" / "inbox.jsonl"
        reply_inbox.parent.mkdir(parents=True)
        reply_inbox.touch()

        intent = IntentBlock(
            goal="Route to node 2", intent_type="route",
            context={"target_node": 2}, success_criteria=[],
            clarifications_needed=[], human_summary="Routing.",
            raw_prompt="What is node 2's balance?",
        )
        result = PipelineResult(
            request_id=1, ts=1000, success=True,
            stage_failed=None, intent=intent, plan=None,
            step_results=[], human_summary="Balance is 500000 sat.",
            error=None, pipeline_build="test",
        )

        mock_coord = MagicMock()
        mock_coord._node = 2
        mock_coord.trace = MagicMock()

        PipelineCoordinator._send_route_reply(mock_coord, "reply-001", str(reply_inbox), result)

        lines = reply_inbox.read_text().strip().split("\n")
        assert len(lines) == 1
        reply = json.loads(lines[0])
        assert reply["in_reply_to"] == "reply-001"
        assert reply["from_node"] == 2
        assert reply["success"] is True
        assert "500000" in reply["content"]

    def test_reply_contains_human_summary(self, tmp_path):
        reply_inbox = tmp_path / "inbox.jsonl"
        reply_inbox.touch()

        result = PipelineResult(
            request_id=1, ts=1000, success=False,
            stage_failed="executor", intent=None, plan=None,
            step_results=[], human_summary="Executor failed: timeout.",
            error="timeout", pipeline_build="test",
        )

        mock_coord = MagicMock()
        mock_coord._node = 3
        mock_coord.trace = MagicMock()

        PipelineCoordinator._send_route_reply(mock_coord, "reply-002", str(reply_inbox), result)

        reply = json.loads(reply_inbox.read_text().strip())
        assert reply["human_summary"] == "Executor failed: timeout."
        assert reply["success"] is False


# =============================================================================
# PIPELINE_BUILD constant
# =============================================================================

class TestPipelineBuild:
    def test_build_string_contains_stages(self):
        assert "translator" in PIPELINE_BUILD
        assert "planner" in PIPELINE_BUILD
        assert "executor" in PIPELINE_BUILD
        assert "summarizer" in PIPELINE_BUILD


# =============================================================================
# _friendly_error() — user-facing error message translation
# =============================================================================

class TestFriendlyError:
    """Verify _friendly_error maps technical errors to helpful messages."""

    def test_auth_error(self):
        msg = _friendly_error("translator", "LLM error during translation: AuthError: Invalid API key")
        assert "API key" in msg
        assert "Settings" in msg
        assert "AuthError" in msg  # raw preserved in parens

    def test_rate_limit_error(self):
        msg = _friendly_error("planner", "LLM error during planning: RateLimitError: 429 Too Many Requests")
        assert "rate-limiting" in msg
        assert "wait" in msg

    def test_transient_api_error(self):
        msg = _friendly_error("translator", "TransientAPIError: 503 Service Unavailable")
        assert "temporarily unavailable" in msg

    def test_mcp_timeout(self):
        msg = _friendly_error("executor", "Step 1 (ln_listfunds) failed: MCP timeout (30s) for tool ln_listfunds")
        assert "Lightning node" in msg
        assert "not respond" in msg

    def test_mcp_client_crash(self):
        msg = _friendly_error("executor", "MCP client error: ConnectionRefusedError: connection refused")
        assert "MCP server" in msg

    def test_json_parse_failure(self):
        msg = _friendly_error("translator", "JSON decode error: Expecting value: line 1 column 1")
        assert "unparseable" in msg
        assert "rephras" in msg

    def test_exhausted_retries(self):
        msg = _friendly_error("translator", "Translator failed after 3 attempts. Last error: invalid JSON")
        assert "multiple attempts" in msg
        assert "simplif" in msg

    def test_fallback_unknown_error(self):
        msg = _friendly_error("planner", "some obscure error nobody anticipated")
        assert msg.startswith("Planner error:")
        assert "some obscure error" in msg

    def test_raw_error_always_preserved(self):
        """The raw error string should always appear in the output for debugging."""
        raw = "LLM error during translation: AuthError: sk-abc...xyz invalid"
        msg = _friendly_error("translator", raw)
        assert raw in msg

    def test_case_insensitive_matching(self):
        msg = _friendly_error("executor", "MCPTIMEOUTERROR: deadline exceeded")
        assert "Lightning node" in msg


# =============================================================================
# TraceLogger — recover_on_startup and rotate_archives
# =============================================================================

class TestTraceLoggerRecovery:
    """Tests for TraceLogger.recover_on_startup()."""

    def test_recover_empty_trace(self, tmp_path):
        trace = TraceLogger(tmp_path / "trace.log")
        # No file exists — nothing to recover
        assert trace.recover_on_startup() is None

    def test_recover_zero_byte_trace(self, tmp_path):
        trace_path = tmp_path / "trace.log"
        trace_path.write_text("")
        trace = TraceLogger(trace_path)
        assert trace.recover_on_startup() is None

    def test_recover_non_empty_trace(self, tmp_path):
        trace_path = tmp_path / "trace.log"
        header = {"event": "prompt_start", "req_id": 7, "ts": 1710000000}
        trace_path.write_text(json.dumps(header) + "\n")
        trace = TraceLogger(trace_path)
        result = trace.recover_on_startup()
        assert result is not None
        assert result.name.startswith("0007_")
        assert result.name.endswith("_recovered.jsonl")
        assert result.exists()

    def test_recover_with_corrupt_header(self, tmp_path):
        trace_path = tmp_path / "trace.log"
        trace_path.write_text("not json\n")
        trace = TraceLogger(trace_path)
        # Should still archive even with unparseable header (uses fallback req_id=0)
        result = trace.recover_on_startup()
        assert result is not None
        assert "_recovered.jsonl" in result.name

    def test_recover_preserves_content(self, tmp_path):
        trace_path = tmp_path / "trace.log"
        lines = [
            json.dumps({"event": "prompt_start", "req_id": 5, "ts": 1710000000}),
            json.dumps({"event": "stage_timing", "req_id": 5}),
        ]
        trace_path.write_text("\n".join(lines) + "\n")
        trace = TraceLogger(trace_path)
        result = trace.recover_on_startup()
        assert result.read_text() == trace_path.read_text()


class TestTraceLoggerRotation:
    """Tests for TraceLogger.rotate_archives()."""

    def _create_archives(self, logs_dir: Path, count: int) -> list:
        logs_dir.mkdir(parents=True, exist_ok=True)
        files = []
        for i in range(count):
            f = logs_dir / f"{i:04d}_20260320-{i:06d}_ok.jsonl"
            f.write_text("{}\n")
            files.append(f)
        return files

    def test_no_rotation_under_limit(self, tmp_path):
        trace = TraceLogger(tmp_path / "trace.log")
        self._create_archives(tmp_path / "logs", 5)
        assert trace.rotate_archives(max_files=10) == 0

    def test_no_rotation_at_limit(self, tmp_path):
        trace = TraceLogger(tmp_path / "trace.log")
        self._create_archives(tmp_path / "logs", 10)
        assert trace.rotate_archives(max_files=10) == 0

    def test_deletes_oldest_over_limit(self, tmp_path):
        trace = TraceLogger(tmp_path / "trace.log")
        files = self._create_archives(tmp_path / "logs", 15)
        deleted = trace.rotate_archives(max_files=10)
        assert deleted == 5
        # Oldest 5 should be gone, newest 10 remain
        for f in files[:5]:
            assert not f.exists()
        for f in files[5:]:
            assert f.exists()

    def test_no_logs_dir(self, tmp_path):
        trace = TraceLogger(tmp_path / "trace.log")
        # logs/ doesn't exist — should return 0, not error
        assert trace.rotate_archives(max_files=10) == 0

    def test_rotation_with_max_one(self, tmp_path):
        trace = TraceLogger(tmp_path / "trace.log")
        files = self._create_archives(tmp_path / "logs", 5)
        deleted = trace.rotate_archives(max_files=1)
        assert deleted == 4
        remaining = list((tmp_path / "logs").glob("*.jsonl"))
        assert len(remaining) == 1
