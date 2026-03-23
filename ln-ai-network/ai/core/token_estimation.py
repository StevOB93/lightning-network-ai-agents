from __future__ import annotations

# =============================================================================
# HeuristicTokenEstimator — conservative token count estimate for rate limiting
#
# Exact token counts are only available after an LLM call completes (from the
# response's usage field). For rate limiting, we need a pre-call estimate to
# decide whether the TPM bucket has enough tokens.
#
# This estimator uses a simple heuristic:
#   chars / 4          — rough average across English text (~4 chars per token)
#   + 50               — fixed overhead for message framing and BOS/EOS tokens
#   + 10 * len(msgs)   — per-message framing overhead (role, separators)
#
# The heuristic intentionally overestimates slightly (conservative) to reduce
# the chance of exceeding provider limits. DualRateLimiter.reconcile_actual()
# can correct underestimates after the call if real usage data is available.
#
# This is NOT a tiktoken / sentencepiece tokenizer — it is a best-effort
# approximation that works well enough for rate-limit budgeting.
# =============================================================================

import json
from typing import Any, Dict, List


class HeuristicTokenEstimator:
    """
    Provider-agnostic, conservative token count estimator.

    Estimates total prompt tokens (messages + tools schema) to pre-check
    whether the TPM bucket has enough capacity before making an LLM call.
    """

    def estimate_prompt_tokens(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
    ) -> int:
        """
        Estimate the number of tokens consumed by the given messages and tools.

        Formula:
          (char_count_of_all_message_fields + char_count_of_tools_json) / 4
          + 50                    (fixed BOS/EOS/instruction framing)
          + 10 * len(messages)    (per-message role/separator tokens)

        Returns an integer token count. Always >= 1.
        """
        # Sum all message field character lengths
        msg_chars = 0
        for m in messages:
            msg_chars += len(str(m.get("role", "")))
            msg_chars += len(str(m.get("name", "")))
            msg_chars += len(str(m.get("content", "")))

        # Serialize the full tools schema and count its characters
        tools_chars = len(json.dumps(tools, ensure_ascii=False)) if tools else 0

        approx_tokens = (msg_chars + tools_chars) // 4
        overhead = 50 + (len(messages) * 10)
        return int(approx_tokens + overhead)
