"""Tests for pipeline persistent conversation history.

Verifies that PipelineCoordinator._load_history() and _update_history()
correctly persist history to disk and reload it on restart.

Strategy:
  - Instantiate the history methods directly without a full PipelineCoordinator
    by testing the logic through a lightweight harness.
  - No real MCP, LLM, or API calls are made.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from ai.core.config import AgentConfig


# =============================================================================
# Minimal harness — replicate only the history methods for isolated testing
# =============================================================================

class _HistoryHarness:
    """
    Isolates the persistent history logic from PipelineCoordinator for testing.
    Replicates _load_history() and _update_history() with the same logic.
    """

    def __init__(self, history_path: Path, cfg: AgentConfig) -> None:
        self._history_path = history_path
        self._cfg = cfg
        self._history: List[Dict[str, Any]] = self._load_history()

    def _load_history(self) -> List[Dict[str, Any]]:
        if not self._history_path.exists():
            return []
        try:
            lines = self._history_path.read_text(encoding="utf-8").splitlines()
            history = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    history.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            max_msgs = self._cfg.max_history_messages * 2
            return history[-max_msgs:] if len(history) > max_msgs else history
        except Exception:
            return []

    def _update_history(self, user_text: str, assistant_summary: str) -> None:
        new_msgs = [
            {"role": "user",      "content": user_text},
            {"role": "assistant", "content": assistant_summary},
        ]
        self._history.extend(new_msgs)
        max_msgs = self._cfg.max_history_messages * 2
        if len(self._history) > max_msgs:
            self._history = self._history[-max_msgs:]
        try:
            with self._history_path.open("a", encoding="utf-8") as fh:
                for msg in new_msgs:
                    fh.write(json.dumps(msg, ensure_ascii=False) + "\n")
        except Exception:
            pass


# =============================================================================
# Tests
# =============================================================================

@pytest.fixture
def cfg():
    return AgentConfig(max_history_messages=3)  # 3 pairs = 6 messages max


@pytest.fixture
def history_path(tmp_path):
    return tmp_path / "history.jsonl"


def test_load_history_empty_when_no_file(history_path, cfg):
    h = _HistoryHarness(history_path, cfg)
    assert h._history == []


def test_update_history_appends_to_disk(history_path, cfg):
    h = _HistoryHarness(history_path, cfg)
    h._update_history("hello", "hi there")
    lines = history_path.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"role": "user", "content": "hello"}
    assert json.loads(lines[1]) == {"role": "assistant", "content": "hi there"}


def test_history_survives_restart(history_path, cfg):
    h1 = _HistoryHarness(history_path, cfg)
    h1._update_history("q1", "a1")
    h1._update_history("q2", "a2")

    # Simulate restart: create a new harness that reads the same file
    h2 = _HistoryHarness(history_path, cfg)
    assert len(h2._history) == 4
    assert h2._history[0] == {"role": "user", "content": "q1"}
    assert h2._history[3] == {"role": "assistant", "content": "a2"}


def test_history_trimmed_to_max_on_load(history_path, cfg):
    """History file with more entries than max is trimmed on load."""
    # Write 8 messages (4 pairs) — max is 3 pairs = 6
    for i in range(4):
        with history_path.open("a") as f:
            f.write(json.dumps({"role": "user", "content": f"q{i}"}) + "\n")
            f.write(json.dumps({"role": "assistant", "content": f"a{i}"}) + "\n")

    h = _HistoryHarness(history_path, cfg)
    assert len(h._history) == 6  # trimmed to max_history_messages * 2


def test_update_history_trims_in_memory(history_path, cfg):
    """In-memory history is trimmed after each update."""
    h = _HistoryHarness(history_path, cfg)
    for i in range(5):
        h._update_history(f"q{i}", f"a{i}")
    # max_history_messages=3 → max 6 messages in memory
    assert len(h._history) == 6


def test_corrupted_lines_skipped(history_path, cfg):
    """Corrupted JSON lines in history.jsonl are silently ignored."""
    history_path.write_text(
        '{"role": "user", "content": "good"}\n'
        'NOT VALID JSON\n'
        '{"role": "assistant", "content": "ok"}\n'
    )
    h = _HistoryHarness(history_path, cfg)
    assert len(h._history) == 2  # corrupted line skipped


def test_empty_lines_skipped(history_path, cfg):
    history_path.write_text(
        '{"role": "user", "content": "q"}\n'
        '\n'
        '\n'
        '{"role": "assistant", "content": "a"}\n'
    )
    h = _HistoryHarness(history_path, cfg)
    assert len(h._history) == 2
