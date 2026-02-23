from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, Literal


# -----------------------------
# Normalized data structures
# -----------------------------

@dataclass(frozen=True)
class LLMUsage:
    prompt_tokens: int
    output_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: Dict[str, Any]


ResponseType = Literal["tool_call", "final"]


@dataclass(frozen=True)
class LLMResponse:
    """
    Provider-agnostic normalized response.

    - type="tool_call": tool_calls contains one or more ToolCall entries
    - type="final": content contains the assistant output

    reasoning is optional (many providers do not expose it).
    usage is optional but strongly recommended (enables token-aware throttling).
    """
    type: ResponseType
    tool_calls: List[ToolCall]
    content: Optional[str]
    reasoning: Optional[str]
    usage: Optional[LLMUsage]


@dataclass(frozen=True)
class LLMRequest:
    messages: List[Dict[str, Any]]
    tools: List[Dict[str, Any]]
    max_output_tokens: int
    temperature: float


# -----------------------------
# Normalized error taxonomy
# -----------------------------

class LLMError(Exception):
    """Base class for all normalized backend errors."""


class RateLimitError(LLMError):
    def __init__(self, message: str = "Rate limited", retry_after_s: Optional[float] = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class TransientAPIError(LLMError):
    """Timeouts, 5xx, connection resets, and other retryable failures."""


class PermanentAPIError(LLMError):
    """4xx that are not expected to succeed on retry (schema/validation)."""


class AuthError(LLMError):
    """Invalid credentials / permissions."""


# -----------------------------
# Token estimation (pluggable)
# -----------------------------

class TokenEstimator(Protocol):
    def estimate_prompt_tokens(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> int: ...


# -----------------------------
# Backend interface
# -----------------------------

class LLMBackend(ABC):
    @abstractmethod
    def step(self, request: LLMRequest) -> LLMResponse:
        """
        Provider-agnostic step call.

        MUST NOT perform any tool execution.
        MUST return a normalized LLMResponse.
        MUST raise only the normalized errors from this module.
        """
        raise NotImplementedError

    def token_estimator(self) -> Optional[TokenEstimator]:
        """
        Optional: backends may provide a better estimator.
        Agent core must work without it.
        """
        return None