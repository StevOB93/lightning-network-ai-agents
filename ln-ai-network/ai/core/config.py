from __future__ import annotations

# =============================================================================
# AgentConfig — central configuration dataclass for the AI agent's runtime
#
# All tunable parameters live here: rate limiting, backoff policy, LLM call
# shaping, and concurrency gates. Each parameter maps to an env var so the
# agent can be reconfigured without code changes.
#
# Wiring: PipelineCoordinator calls AgentConfig.from_env() at startup and uses
# cfg.tick_ms to drive DeterministicScheduler. The core/* modules (rate_limiter,
# backoff, scheduler) also consume these values directly.
#
# Env vars (all optional; defaults match the dataclass field defaults):
#   AGENT_TICK_MS                — scheduler cadence in milliseconds
#   LLM_MIN_INTERVAL_MS          — minimum gap between consecutive LLM calls
#   LLM_MAX_IN_FLIGHT            — max concurrent LLM calls (semaphore)
#   LLM_RPM                      — requests per minute (rate limit bucket)
#   LLM_TPM                      — tokens per minute (rate limit bucket)
#   LLM_TEMPERATURE              — sampling temperature for all LLM stages
#   LLM_MAX_OUTPUT_TOKENS        — max tokens per LLM response
#   LLM_MAX_HISTORY_MESSAGES     — sliding window for conversation history
#   LLM_MAX_TOOL_OUTPUT_CHARS    — max chars of tool output kept in context
#   LLM_BACKOFF_BASE_MS          — backoff base delay in milliseconds
#   LLM_BACKOFF_MAX_MS           — backoff cap in milliseconds
#   LLM_BACKOFF_JITTER_MS        — max deterministic jitter added to backoff
#   LLM_CIRCUIT_BREAKER_AFTER    — consecutive failures before circuit opens
#   LLM_CIRCUIT_BREAKER_OPEN_MS  — duration to keep the circuit open
# =============================================================================

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    """Read an integer env var; return default if absent, blank, or non-numeric."""
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    try:
        return int(v)
    except (ValueError, TypeError):
        return default


def _env_float(name: str, default: float) -> float:
    """Read a float env var; return default if absent, blank, or non-numeric."""
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


@dataclass(frozen=True)
class AgentConfig:
    """
    Immutable configuration snapshot for one agent session.

    Frozen so that config cannot be mutated mid-run (changing rate limits or
    backoff parameters after the buckets are constructed would leave the system
    in an inconsistent state).

    Use AgentConfig.from_env() to build from environment variables.
    Use AgentConfig() directly in tests with custom values.
    """

    # ── Scheduler ─────────────────────────────────────────────────────────────
    # How often the agent's main loop checks for new inbox messages.
    tick_ms: int = 500

    # ── Concurrency ───────────────────────────────────────────────────────────
    # Minimum wall-clock gap between consecutive LLM API calls. Prevents
    # back-to-back calls in rapid succession (e.g. retries) from hammering
    # the provider faster than RPM/TPM limits allow.
    llm_min_interval_ms: int = 1000

    # Maximum simultaneous in-flight LLM requests. Set to 1 for the default
    # single-threaded loop; increase if the agent is ever parallelized.
    llm_max_in_flight: int = 1

    # ── Rate limits ───────────────────────────────────────────────────────────
    # Set these to match your provider quota allocation. The DualRateLimiter
    # uses token buckets that refill at rpm/60 per second and tpm/60 per second.
    llm_rpm: int = 30        # requests per minute
    llm_tpm: int = 60_000   # tokens per minute

    # ── LLM request shaping ───────────────────────────────────────────────────
    # Low temperature (0.2) keeps output deterministic and structured.
    # max_output_tokens: keep short — each stage has its own specific output.
    llm_temperature: float = 0.2
    llm_max_output_tokens: int = 512

    # ── Prompt growth control ─────────────────────────────────────────────────
    # Bound context window usage. max_history_messages is a sliding window;
    # max_tool_output_chars truncates tool output before injecting into history.
    max_history_messages: int = 6
    max_tool_output_chars: int = 8_000

    # ── Backoff policy ────────────────────────────────────────────────────────
    # Exponential backoff: delay = min(max, base * 2^attempt) + jitter
    # jitter is deterministic (hash-based) so behavior is reproducible.
    backoff_base_ms: int = 1_000
    backoff_max_ms: int = 30_000
    backoff_jitter_ms: int = 250

    # Circuit breaker: after N consecutive failures, block all calls for
    # circuit_breaker_open_ms milliseconds, then reset attempt count.
    circuit_breaker_after: int = 6
    circuit_breaker_open_ms: int = 60_000

    # ── Payment strategy ───────────────────────────────────────────────────
    # Default strategy when the UI doesn't send one. Options:
    # cheap, fast, detailed, max_effort
    default_payment_strategy: str = "fast"

    @staticmethod
    def from_env() -> "AgentConfig":
        """
        Construct an AgentConfig from environment variables.

        Each field has a corresponding env var (see module docstring). Values
        absent or blank in the environment fall back to the field defaults.
        Invalid (non-numeric) values raise ValueError — this is intentional:
        misconfigured deployments should crash loudly at startup.
        """
        return AgentConfig(
            tick_ms=_env_int("AGENT_TICK_MS", 500),
            llm_min_interval_ms=_env_int("LLM_MIN_INTERVAL_MS", 1000),
            llm_max_in_flight=_env_int("LLM_MAX_IN_FLIGHT", 1),
            llm_rpm=_env_int("LLM_RPM", 30),
            llm_tpm=_env_int("LLM_TPM", 60_000),
            llm_temperature=_env_float("LLM_TEMPERATURE", 0.2),
            llm_max_output_tokens=_env_int("LLM_MAX_OUTPUT_TOKENS", 512),
            max_history_messages=_env_int("LLM_MAX_HISTORY_MESSAGES", 6),
            max_tool_output_chars=_env_int("LLM_MAX_TOOL_OUTPUT_CHARS", 8_000),
            backoff_base_ms=_env_int("LLM_BACKOFF_BASE_MS", 1000),
            backoff_max_ms=_env_int("LLM_BACKOFF_MAX_MS", 30_000),
            backoff_jitter_ms=_env_int("LLM_BACKOFF_JITTER_MS", 250),
            circuit_breaker_after=_env_int("LLM_CIRCUIT_BREAKER_AFTER", 6),
            circuit_breaker_open_ms=_env_int("LLM_CIRCUIT_BREAKER_OPEN_MS", 60_000),
            default_payment_strategy=os.getenv("DEFAULT_PAYMENT_STRATEGY", "fast"),
        )
