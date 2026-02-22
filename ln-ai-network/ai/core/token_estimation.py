from __future__ import annotations

import json
from typing import Any, Dict, List


class HeuristicTokenEstimator:
    """
    Provider-agnostic, conservative-ish estimate.
    Not perfect, but good enough to prevent TPM flooding.
    """

    def estimate_prompt_tokens(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> int:
        # Rough heuristic: ~4 chars per token + overhead.
        # Add a fixed overhead per message + tools schema size.
        msg_chars = 0
        for m in messages:
            msg_chars += len(str(m.get("role", "")))
            msg_chars += len(str(m.get("name", "")))
            msg_chars += len(str(m.get("content", "")))

        tools_chars = len(json.dumps(tools, ensure_ascii=False)) if tools else 0

        approx_tokens = (msg_chars + tools_chars) // 4
        overhead = 50 + (len(messages) * 10)
        return int(approx_tokens + overhead)