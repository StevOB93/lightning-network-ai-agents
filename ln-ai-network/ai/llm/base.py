from __future__ import annotations

# =============================================================================
# ai.llm.base — provider-agnostic LLM interface
#
# This module defines the contract that all LLM backends must implement.
# Pipeline stages and the agent only ever import from this module — they never
# import from a specific adapter. This makes the backend swappable at runtime
# via environment variable (see ai.llm.factory).
#
# Data flow per LLM step:
#   LLMRequest  →  LLMBackend.step()  →  LLMResponse
#
# LLMResponse has two shapes:
#   type="tool_call"  — LLM wants to call one or more tools; tool_calls is populated
#   type="final"      — LLM has produced a text answer; content is populated
#
# Error taxonomy:
#   All backends MUST raise only the normalized error subclasses defined here.
#   This lets the caller handle rate limits and transient failures uniformly
#   without knowing which provider is in use.
#
# Token estimation:
#   Backends may optionally provide a TokenEstimator for more accurate RPM/TPM
#   accounting. If not provided, the caller uses HeuristicTokenEstimator
#   (ai.core.token_estimation) as a fallback.
# =============================================================================

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, Literal


# =============================================================================
# Normalized data structures
# =============================================================================

@dataclass(frozen=True)
class LLMUsage:
    """
    Token usage reported by the provider after a completion.

    Frozen so it can be safely shared/stored without defensive copies.
    All fields are integers (provider fractional counts are rounded).
    """
    prompt_tokens: int    # Tokens in the input (messages + tools schema)
    output_tokens: int    # Tokens in the response (content + tool call JSON)
    total_tokens: int     # prompt_tokens + output_tokens


@dataclass(frozen=True)
class ToolCall:
    """
    A single tool invocation requested by the LLM.

    name — must match an MCP tool name (validated by _normalize_tool_args)
    args — decoded argument dict (never a raw JSON string)
    """
    name: str
    args: Dict[str, Any]


# Literal type constrains the LLMResponse.type field to exactly these two strings
ResponseType = Literal["tool_call", "final"]


@dataclass(frozen=True)
class LLMResponse:
    """
    Provider-agnostic normalized response from a single LLM step.

    type="tool_call":
      tool_calls contains one or more ToolCall entries.
      content may be None (most providers) or a partial reasoning string.

    type="final":
      content contains the assistant's text output.
      tool_calls is an empty list.

    reasoning:
      Optional extended thinking / chain-of-thought string. Most providers
      do not expose this; only Claude extended thinking and o-series models do.

    usage:
      Optional but strongly recommended. Used by DualRateLimiter for accurate
      TPM accounting. If None, the caller falls back to an estimate.
    """
    type: ResponseType
    tool_calls: List[ToolCall]
    content: Optional[str]
    reasoning: Optional[str]
    usage: Optional[LLMUsage]


@dataclass(frozen=True)
class LLMRequest:
    """
    Input to a single LLM step. Passed to LLMBackend.step().

    messages — full conversation history in OpenAI message format:
               [{"role": "system"|"user"|"assistant"|"tool", "content": "..."}]
    tools    — list of tool schemas in OpenAI function-calling format.
               Empty list = no tool use; LLM will produce a text response.
    max_output_tokens — hard cap on the response length (not counting reasoning)
    temperature       — 0.0 = deterministic greedy; higher = more creative
    """
    messages: List[Dict[str, Any]]
    tools: List[Dict[str, Any]]
    max_output_tokens: int
    temperature: float


# =============================================================================
# Normalized error taxonomy
# =============================================================================

class LLMError(Exception):
    """Base class for all normalized backend errors. Callers should catch this."""


class RateLimitError(LLMError):
    """
    Provider is rate-limiting this client (HTTP 429 or equivalent).

    retry_after_s: if the provider returned a Retry-After header, this is
    the suggested wait time in seconds. May be None if not provided.
    """
    def __init__(self, message: str = "Rate limited", retry_after_s: Optional[float] = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class TransientAPIError(LLMError):
    """
    Retryable failure: timeouts, 5xx server errors, connection resets.
    The caller should retry after a backoff delay.
    """


class PermanentAPIError(LLMError):
    """
    Non-retryable failure: 4xx errors that indicate a bad request (schema
    mismatch, invalid model name, context length exceeded, etc.).
    Retrying the same request without modification will not help.
    """


class AuthError(LLMError):
    """Invalid or missing API credentials. Retrying will not help."""


# =============================================================================
# Token estimation (pluggable)
# =============================================================================

class TokenEstimator(Protocol):
    """
    Structural protocol for token count estimation before a request is sent.

    Backends may implement this to provide provider-specific counting (e.g.
    using tiktoken for OpenAI). If not implemented, HeuristicTokenEstimator
    from ai.core.token_estimation is used as a conservative fallback.
    """
    def estimate_prompt_tokens(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> int: ...


# =============================================================================
# Backend interface
# =============================================================================

class LLMBackend(ABC):
    """
    Abstract base class for all LLM provider adapters.

    Implementations (in ai.llm.adapters/):
      OllamaBackend  — local Ollama server via HTTP
      OpenAIBackend  — OpenAI API (also compatible with Azure OpenAI)
      GeminiBackend  — Google Gemini API

    Contract:
      - step() MUST NOT execute tool calls — that's the agent/pipeline's job.
      - step() MUST return a normalized LLMResponse.
      - step() MUST only raise the normalized error classes from this module.
      - token_estimator() is optional; return None if not implemented.
    """

    @abstractmethod
    def step(self, request: LLMRequest) -> LLMResponse:
        """
        Send a request to the provider and return a normalized response.
        Raises LLMError subclasses on failure — never raw provider exceptions.
        """
        raise NotImplementedError

    def token_estimator(self) -> Optional[TokenEstimator]:
        """
        Return a provider-specific token estimator, or None to use the heuristic.
        Backends that know their exact tokenizer should override this.
        """
        return None
