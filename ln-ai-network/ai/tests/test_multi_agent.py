"""Tests for multi-agent infrastructure.

Covers:
  - Per-agent queue directories (command_queue with agent_id)
  - Inter-agent routing via AgentRegistry (route_to + await_reply)
  - Pipeline route intent handling (_handle_route_intent)
  - Pipeline reply sending (_send_route_reply)
  - Translator route intent type recognition

All tests are self-contained with no external dependencies or live
infrastructure.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch


# Ensure the repo root is on sys.path
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ai.command_queue import (
    QueuePaths,
    ensure,
    enqueue,
    last_outbox,
    paths,
    read_new,
    write_outbox,
)
from ai.core.registry import AgentRegistry
from ai.models import IntentBlock, PipelineResult


# =============================================================================
# Per-agent queue directories
# =============================================================================

class TestPerAgentQueues:
    """command_queue functions produce isolated directories when agent_id is set."""

    def test_paths_default_no_agent_id(self):
        """Default (None) agent_id uses runtime/agent/."""
        qp = paths()
        assert qp.base_dir.name == "agent"
        assert "agent-" not in str(qp.base_dir)

    def test_paths_with_agent_id(self):
        """Agent ID produces runtime/agent-{id}/ directory."""
        qp = paths(agent_id="2")
        assert qp.base_dir.name == "agent-2"
        assert qp.inbox.parent.name == "agent-2"

    def test_paths_different_agents_are_isolated(self):
        """Two different agent IDs produce different directories."""
        qp1 = paths(agent_id="1")
        qp2 = paths(agent_id="2")
        assert qp1.base_dir != qp2.base_dir
        assert qp1.inbox != qp2.inbox

    def test_ensure_creates_agent_dir(self, tmp_path, monkeypatch):
        """ensure(agent_id) creates the agent-specific directory."""
        monkeypatch.setattr("ai.command_queue._repo_root", lambda: tmp_path)
        qp = ensure(agent_id="3")
        assert qp.base_dir.exists()
        assert qp.base_dir.name == "agent-3"
        assert qp.inbox.exists()
        assert qp.outbox.exists()

    def test_enqueue_to_specific_agent(self, tmp_path, monkeypatch):
        """enqueue() writes to the correct agent's inbox."""
        monkeypatch.setattr("ai.command_queue._repo_root", lambda: tmp_path)
        msg = enqueue("hello agent 2", agent_id="2")
        assert msg["content"] == "hello agent 2"
        inbox = tmp_path / "runtime" / "agent-2" / "inbox.jsonl"
        assert inbox.exists()
        data = json.loads(inbox.read_text().strip())
        assert data["content"] == "hello agent 2"

    def test_read_new_from_specific_agent(self, tmp_path, monkeypatch):
        """read_new() reads from the correct agent's inbox."""
        monkeypatch.setattr("ai.command_queue._repo_root", lambda: tmp_path)
        enqueue("msg for agent 1", agent_id="1")
        enqueue("msg for agent 2", agent_id="2")
        msgs_1 = read_new(agent_id="1")
        msgs_2 = read_new(agent_id="2")
        assert len(msgs_1) == 1
        assert len(msgs_2) == 1
        assert msgs_1[0]["content"] == "msg for agent 1"
        assert msgs_2[0]["content"] == "msg for agent 2"

    def test_write_outbox_to_specific_agent(self, tmp_path, monkeypatch):
        """write_outbox() writes to the correct agent's outbox."""
        monkeypatch.setattr("ai.command_queue._repo_root", lambda: tmp_path)
        ensure(agent_id="5")
        write_outbox({"content": "result"}, agent_id="5")
        outbox = tmp_path / "runtime" / "agent-5" / "outbox.jsonl"
        assert outbox.exists()
        data = json.loads(outbox.read_text().strip())
        assert data["content"] == "result"

    def test_last_outbox_from_specific_agent(self, tmp_path, monkeypatch):
        """last_outbox() reads from the correct agent's outbox."""
        monkeypatch.setattr("ai.command_queue._repo_root", lambda: tmp_path)
        ensure(agent_id="7")
        write_outbox({"content": "first"}, agent_id="7")
        write_outbox({"content": "second"}, agent_id="7")
        result = last_outbox(agent_id="7")
        assert result is not None
        assert result["content"] == "second"

    def test_agents_dont_cross_read(self, tmp_path, monkeypatch):
        """Messages enqueued for one agent aren't visible to another."""
        monkeypatch.setattr("ai.command_queue._repo_root", lambda: tmp_path)
        enqueue("only for agent 1", agent_id="1")
        msgs = read_new(agent_id="2")
        assert msgs == []


# =============================================================================
# Inter-agent routing via registry
# =============================================================================

class TestInterAgentRouting:
    """End-to-end routing between two agents via the shared registry."""

    def test_route_and_reply(self, tmp_path):
        """Agent 1 routes a message to agent 2 and receives a reply."""
        registry = AgentRegistry(tmp_path / "registry.jsonl")

        # Set up agent 1's inbox
        inbox_1 = tmp_path / "agent-1" / "inbox.jsonl"
        inbox_1.parent.mkdir(parents=True)
        inbox_1.touch()

        # Set up agent 2's inbox
        inbox_2 = tmp_path / "agent-2" / "inbox.jsonl"
        inbox_2.parent.mkdir(parents=True)
        inbox_2.touch()

        # Register both agents
        registry.register("pipeline", node=1, inbox_path=inbox_1)
        registry.register("pipeline", node=2, inbox_path=inbox_2)

        # Agent 1 sends a routed message to agent 2
        reply_id = "test-route-001"
        msg = {
            "id": 42,
            "content": "What is node 2's balance?",
            "meta": {"kind": "freeform", "use_llm": True},
            "reply_id": reply_id,
            "reply_inbox": str(inbox_1),
            "routed_from_node": 1,
        }
        ok = registry.route_to("pipeline", node=2, message=msg)
        assert ok is True

        # Verify message landed in agent 2's inbox
        lines = inbox_2.read_text().strip().split("\n")
        assert len(lines) == 1
        routed = json.loads(lines[0])
        assert routed["content"] == "What is node 2's balance?"
        assert routed["reply_id"] == reply_id

        # Simulate agent 2 writing a reply back to agent 1's inbox
        reply = {
            "in_reply_to": reply_id,
            "content": "Node 2 balance: 500000 sat",
            "from_node": 2,
        }
        with inbox_1.open("a", encoding="utf-8") as f:
            f.write(json.dumps(reply) + "\n")

        # Agent 1 awaits the reply
        result = registry.await_reply(reply_id, inbox_1, timeout_s=1.0)
        assert result is not None
        assert result["content"] == "Node 2 balance: 500000 sat"
        assert result["from_node"] == 2

    def test_route_fails_when_no_target(self, tmp_path):
        """Routing to a non-existent node returns False."""
        registry = AgentRegistry(tmp_path / "registry.jsonl")
        ok = registry.route_to("pipeline", node=99, message={"content": "hi"})
        assert ok is False

    def test_multiple_agents_coexist(self, tmp_path):
        """Multiple agents with distinct PIDs all appear in list_peers.

        In production each agent is a separate process. The registry deduplicates
        by PID (most recent wins), so we manually write entries. We use our own
        PID for node 1 (guaranteed alive) and verify the others are filtered
        correctly (dead PIDs are excluded).
        """
        registry = AgentRegistry(tmp_path / "registry.jsonl")

        # Write entries: our own PID for node 1, a dead PID for node 2
        entries = [
            {"kind": "pipeline", "node": 1, "pid": os.getpid(),
             "inbox": str(tmp_path / "agent-1" / "inbox.jsonl"), "ts": 100},
            {"kind": "pipeline", "node": 2, "pid": 99_999_999,
             "inbox": str(tmp_path / "agent-2" / "inbox.jsonl"), "ts": 101},
        ]
        with registry.path.open("w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        # Only node 1 (our PID) should appear — node 2's PID is dead
        peers = registry.list_peers()
        assert len(peers) == 1
        assert peers[0]["node"] == 1

    def test_route_between_live_agents(self, tmp_path):
        """A live agent can route to another live agent (same PID in test, different node)."""
        registry = AgentRegistry(tmp_path / "registry.jsonl")
        inbox_2 = tmp_path / "agent-2" / "inbox.jsonl"
        inbox_2.parent.mkdir(parents=True)
        inbox_2.touch()

        # Register node 2 (uses our PID — alive by definition)
        registry.register("pipeline", node=2, inbox_path=inbox_2)

        # Route a message to node 2
        ok = registry.route_to("pipeline", node=2, message={"content": "query for node 2"})
        assert ok is True
        routed = json.loads(inbox_2.read_text().strip())
        assert routed["content"] == "query for node 2"


# =============================================================================
# Pipeline route intent handling
# =============================================================================

class TestPipelineRouteIntent:
    """Tests for _handle_route_intent and _send_route_reply on PipelineCoordinator."""

    def _make_intent(self, target_node: int = 2, routed_prompt: str = "check balance") -> IntentBlock:
        return IntentBlock(
            goal=f"Route query to node {target_node}",
            intent_type="route",
            context={"target_node": target_node, "routed_prompt": routed_prompt},
            success_criteria=[],
            clarifications_needed=[],
            human_summary=f"Routing to node {target_node}.",
            raw_prompt=f"What is node {target_node}'s balance?",
        )

    def test_send_route_reply_writes_to_inbox(self, tmp_path):
        """_send_route_reply writes a JSON reply to the specified inbox path."""
        from ai.pipeline import PipelineCoordinator

        reply_inbox = tmp_path / "sender" / "inbox.jsonl"
        reply_inbox.parent.mkdir(parents=True)
        reply_inbox.touch()

        result = PipelineResult(
            request_id=1, ts=int(time.time()), success=True,
            stage_failed=None, intent=self._make_intent(), plan=None,
            step_results=[], human_summary="Balance is 500000 sat.",
            error=None, pipeline_build="test",
        )

        # We can't easily instantiate a full PipelineCoordinator in tests
        # (it acquires locks, creates MCP clients, etc.), so we test the
        # reply-writing logic directly via a mock.
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

    def test_handle_route_intent_no_target_node(self, tmp_path):
        """_handle_route_intent returns failure when target_node is missing."""
        from ai.pipeline import PipelineCoordinator

        intent = IntentBlock(
            goal="Route somewhere",
            intent_type="route",
            context={},  # No target_node
            success_criteria=[],
            clarifications_needed=[],
            human_summary="Routing.",
            raw_prompt="do something on node X",
        )

        mock_coord = MagicMock()
        mock_coord._node = 1
        mock_coord._agent_id = None
        mock_coord._multi_agent = False
        mock_coord._registry = MagicMock()
        mock_coord.trace = MagicMock()

        result = PipelineCoordinator._handle_route_intent(
            mock_coord, intent, req_id=1, ts=int(time.time()), t_translate=10.0,
        )
        assert result.success is False
        assert "target_node" in result.human_summary.lower()

    def test_handle_route_intent_no_live_peer(self, tmp_path):
        """_handle_route_intent returns failure when the target agent isn't registered."""
        from ai.pipeline import PipelineCoordinator

        intent = self._make_intent(target_node=5)

        mock_coord = MagicMock()
        mock_coord._node = 1
        mock_coord._agent_id = "1"
        mock_coord._multi_agent = True
        mock_coord._registry = AgentRegistry(tmp_path / "registry.jsonl")
        mock_coord.trace = MagicMock()

        with patch("ai.pipeline.queue_paths") as mock_qp:
            mock_qp.return_value = QueuePaths(
                base_dir=tmp_path / "agent-1",
                inbox=tmp_path / "agent-1" / "inbox.jsonl",
                outbox=tmp_path / "agent-1" / "outbox.jsonl",
                offset=tmp_path / "agent-1" / "inbox.offset",
                counter=tmp_path / "agent-1" / "msg.counter",
            )
            (tmp_path / "agent-1").mkdir(parents=True, exist_ok=True)
            (tmp_path / "agent-1" / "inbox.jsonl").touch()

            result = PipelineCoordinator._handle_route_intent(
                mock_coord, intent, req_id=1, ts=int(time.time()), t_translate=10.0,
            )
        assert result.success is False
        assert "no live pipeline" in result.human_summary.lower()


# =============================================================================
# Translator route intent type
# =============================================================================

class TestTranslatorRouteType:
    """Verify the Translator recognizes 'route' as a valid intent type."""

    def test_route_is_valid_intent_type(self):
        from ai.controllers.translator import _VALID_INTENT_TYPES
        assert "route" in _VALID_INTENT_TYPES

    def test_route_intent_not_coerced(self):
        """An LLM response with intent_type='route' is preserved, not coerced to freeform."""
        from ai.controllers.translator import _VALID_INTENT_TYPES
        # Just confirm it's in the valid set — the Translator._parse_intent_block
        # coerces unknown types to "freeform" but preserves valid ones.
        assert "route" in _VALID_INTENT_TYPES


# =============================================================================
# Runtime agent dir parameterization
# =============================================================================

class TestRuntimeAgentDir:
    """Verify _runtime_agent_dir() supports multi-agent paths."""

    def test_default_returns_agent_dir(self):
        from ai.utils import _runtime_agent_dir
        d = _runtime_agent_dir()
        assert d.name == "agent"

    def test_with_agent_id(self):
        from ai.utils import _runtime_agent_dir
        d = _runtime_agent_dir(agent_id="3")
        assert d.name == "agent-3"

    def test_none_agent_id_same_as_default(self):
        from ai.utils import _runtime_agent_dir
        assert _runtime_agent_dir(None) == _runtime_agent_dir()
