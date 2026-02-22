from __future__ import annotations

import json
import time
import traceback
from typing import Any, Dict, List

from ai.core.backoff import DeterministicBackoff
from ai.core.config import AgentConfig
from ai.core.concurrency import ConcurrencyGate
from ai.core.rate_limiter import DualRateLimiter
from ai.core.scheduler import DeterministicScheduler
from ai.core.token_estimation import HeuristicTokenEstimator
from ai.llm.base import (
    AuthError,
    LLMRequest,
    PermanentAPIError,
    RateLimitError,
    TransientAPIError,
)
from ai.llm.factory import create_backend
from ai.mcp_client import FastMCPClientWrapper, MCPClient
from mcp.client.fastmcp import FastMCPClient


class LightningAgent:
    """
    Production-oriented deterministic control loop:

    Tick -> (Backoff Gate) -> (RateLimit Gate RPM/TPM/min-interval) -> (Concurrency Gate)
         -> LLM step (provider-agnostic) -> MCP tool execution (ONLY boundary) -> state update

    One LLM request per tick at most.
    """

    def __init__(self) -> None:
        self.cfg = AgentConfig.from_env()

        # MCP boundary (only executor)
        self.mcp: MCPClient = FastMCPClientWrapper(FastMCPClient())

        # Provider backend (swappable via factory)
        self.backend = create_backend()

        # Token estimator (backend may provide a better one)
        self.token_estimator = self.backend.token_estimator() or HeuristicTokenEstimator()

        # Control plane
        self.scheduler = DeterministicScheduler(self.cfg.tick_ms)
        self.limiter = DualRateLimiter(
            rpm=self.cfg.llm_rpm,
            tpm=self.cfg.llm_tpm,
            min_interval_ms=self.cfg.llm_min_interval_ms,
        )
        self.backoff = DeterministicBackoff(
            base_ms=self.cfg.backoff_base_ms,
            max_ms=self.cfg.backoff_max_ms,
            jitter_ms=self.cfg.backoff_jitter_ms,
            circuit_breaker_after=self.cfg.circuit_breaker_after,
            circuit_breaker_open_ms=self.cfg.circuit_breaker_open_ms,
        )
        self.llm_gate = ConcurrencyGate(self.cfg.llm_max_in_flight)

        # Conversation state
        self.messages: List[Dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "You are an autonomous Lightning Network agent.\n"
                    "Rules:\n"
                    "- You MUST only act using the provided tools.\n"
                    "- You MUST NOT assume access outside the tools.\n"
                    "- You MUST NOT call Lightning RPC directly.\n"
                    "- All execution is through MCP tools only.\n"
                    "- If uncertain, ask for clarification or remain idle.\n"
                ),
            }
        ]

        self._step_id = 0

    def get_tools(self) -> List[Dict[str, Any]]:
        # Keep this deterministic. Expand later from intents.schema.json if desired.
        return [
            {
                "type": "function",
                "function": {
                    "name": "network_health",
                    "description": "Check health of Lightning network.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

    def _trim_history(self) -> None:
        """
        Deterministic cap to prevent TPM runaway.
        Keep the first system message + last N-1 messages.
        """
        cap = max(2, int(self.cfg.max_history_messages))
        if len(self.messages) <= cap:
            return
        system = self.messages[0:1]
        tail = self.messages[-(cap - 1):]
        self.messages = system + tail

    def _compact_tool_result(self, result: Any) -> str:
        """
        Deterministic tool output compaction to prevent huge tool payloads.
        """
        s = json.dumps(result, ensure_ascii=False)
        limit = int(self.cfg.max_tool_output_chars)
        if limit <= 0 or len(s) <= limit:
            return s

        head_len = max(0, limit // 2)
        tail_len = max(0, limit - head_len)

        compact = {
            "_truncated": True,
            "original_len": len(s),
            "head": s[:head_len],
            "tail": s[-tail_len:] if tail_len > 0 else "",
        }
        return json.dumps(compact, ensure_ascii=False)

    def _print_event(self, kind: str, payload: Dict[str, Any]) -> None:
        # Deterministic structured logging.
        out = {"ts": int(time.time()), "kind": kind, **payload}
        print(json.dumps(out, ensure_ascii=False))

    def run(self) -> None:
        self._print_event("agent_start", {"msg": "Persistent deterministic control loop started."})

        while True:
            self.scheduler.wait_next_tick()
            self._step_id += 1

            try:
                # Backoff gate
                if self.backoff.blocked():
                    self._print_event("blocked_backoff", {"step": self._step_id})
                    continue

                tools = self.get_tools()

                # Estimate tokens for gating
                est_prompt = self.token_estimator.estimate_prompt_tokens(self.messages, tools)
                est_total = est_prompt + int(self.cfg.llm_max_output_tokens)

                # Rate limit gate
                if not self.limiter.allowed(est_total):
                    self._print_event("blocked_ratelimit", {"step": self._step_id, "est_total_tokens": est_total})
                    continue

                # Concurrency gate (non-blocking; deterministic)
                if not self.llm_gate.acquire(blocking=False):
                    self._print_event("blocked_concurrency", {"step": self._step_id})
                    continue

                try:
                    # Reserve budget before the call (prevents burst spikes)
                    self.limiter.spend(est_total)

                    req = LLMRequest(
                        messages=self.messages,
                        tools=tools,
                        max_output_tokens=self.cfg.llm_max_output_tokens,
                        temperature=self.cfg.llm_temperature,
                    )

                    resp = self.backend.step(req)

                finally:
                    self.llm_gate.release()

                # If we got usage, reconcile token bucket
                if resp.usage is not None:
                    self.limiter.reconcile_actual(
                        actual_total_tokens=resp.usage.total_tokens,
                        estimated_total_tokens=est_total,
                    )

                # Success resets backoff
                self.backoff.note_success()

                if resp.type == "tool_call":
                    # Append assistant "reasoning" (if any)
                    self.messages.append({"role": "assistant", "content": resp.reasoning or ""})

                    # Execute ALL tool calls via MCP, deterministically in order
                    for tc in resp.tool_calls:
                        result = self.mcp.call(tc.name, tc.args)

                        self.messages.append(
                            {
                                "role": "tool",
                                "name": tc.name,
                                "content": self._compact_tool_result(result),
                            }
                        )

                    self._trim_history()

                    self._print_event(
                        "llm_tool_call",
                        {
                            "step": self._step_id,
                            "tools": [{"name": t.name, "args": t.args} for t in resp.tool_calls],
                            "usage": resp.usage.__dict__ if resp.usage else None,
                        },
                    )

                else:
                    self.messages.append({"role": "assistant", "content": resp.content or ""})
                    self._trim_history()

                    self._print_event(
                        "llm_final",
                        {
                            "step": self._step_id,
                            "content": resp.content,
                            "usage": resp.usage.__dict__ if resp.usage else None,
                        },
                    )

            except KeyboardInterrupt:
                self._print_event("agent_stop", {"msg": "Shutdown requested."})
                break

            except RateLimitError as e:
                self._print_event("llm_rate_limited", {"step": self._step_id, "retry_after_s": e.retry_after_s})
                self.backoff.note_failure(self._step_id, retry_after_s=e.retry_after_s)

            except TransientAPIError as e:
                self._print_event("llm_transient_error", {"step": self._step_id, "error": str(e)})
                self.backoff.note_failure(self._step_id)

            except PermanentAPIError as e:
                # Treat as "stop making requests" to avoid flooding a bad schema/config.
                self._print_event("llm_permanent_error", {"step": self._step_id, "error": str(e)})
                self.backoff.note_failure(self._step_id, retry_after_s=60.0)

            except AuthError as e:
                # Auth errors should not be retried quickly.
                self._print_event("llm_auth_error", {"step": self._step_id, "error": str(e)})
                self.backoff.note_failure(self._step_id, retry_after_s=300.0)

            except Exception:
                self._print_event("agent_unhandled_exception", {"step": self._step_id})
                traceback.print_exc()
                self.backoff.note_failure(self._step_id)


if __name__ == "__main__":
    LightningAgent().run()