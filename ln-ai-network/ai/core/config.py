from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    return default if v is None or v.strip() == "" else int(v)


def _env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    return default if v is None or v.strip() == "" else float(v)


@dataclass(frozen=True)
class AgentConfig:
    # Deterministic cadence
    tick_ms: int = 500

    # Hard gates
    llm_min_interval_ms: int = 1000
    llm_max_in_flight: int = 1

    # Rate limits (set these to your provider allocation)
    llm_rpm: int = 30
    llm_tpm: int = 60_000

    # LLM request shaping
    llm_temperature: float = 0.2
    llm_max_output_tokens: int = 512

    # Prompt growth control (token stability)
    max_history_messages: int = 40
    max_tool_output_chars: int = 8_000

    # Backoff policy
    backoff_base_ms: int = 1000
    backoff_max_ms: int = 30_000
    backoff_jitter_ms: int = 250
    circuit_breaker_after: int = 6
    circuit_breaker_open_ms: int = 60_000

    @staticmethod
    def from_env() -> "AgentConfig":
        return AgentConfig(
            tick_ms=_env_int("AGENT_TICK_MS", 500),
            llm_min_interval_ms=_env_int("LLM_MIN_INTERVAL_MS", 1000),
            llm_max_in_flight=_env_int("LLM_MAX_IN_FLIGHT", 1),
            llm_rpm=_env_int("LLM_RPM", 30),
            llm_tpm=_env_int("LLM_TPM", 60_000),
            llm_temperature=_env_float("LLM_TEMPERATURE", 0.2),
            llm_max_output_tokens=_env_int("LLM_MAX_OUTPUT_TOKENS", 512),
            max_history_messages=_env_int("LLM_MAX_HISTORY_MESSAGES", 40),
            max_tool_output_chars=_env_int("LLM_MAX_TOOL_OUTPUT_CHARS", 8_000),
            backoff_base_ms=_env_int("LLM_BACKOFF_BASE_MS", 1000),
            backoff_max_ms=_env_int("LLM_BACKOFF_MAX_MS", 30_000),
            backoff_jitter_ms=_env_int("LLM_BACKOFF_JITTER_MS", 250),
            circuit_breaker_after=_env_int("LLM_CIRCUIT_BREAKER_AFTER", 6),
            circuit_breaker_open_ms=_env_int("LLM_CIRCUIT_BREAKER_OPEN_MS", 60_000),
        )