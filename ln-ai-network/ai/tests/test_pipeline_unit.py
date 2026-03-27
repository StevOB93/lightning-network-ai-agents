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

from ai.core.config import AgentConfig
from ai.models import IntentBlock, PipelineResult
from ai.pipeline import PIPELINE_BUILD, PipelineCoordinator, _VERIFY_TOOL


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
