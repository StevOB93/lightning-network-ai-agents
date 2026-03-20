# =============================================================================
# Manual smoke test for LLM backend adapters
#
# Sends a minimal "Return exactly: OK" request to the configured backend and
# prints the normalized response fields. Use this to verify that an adapter
# is correctly installed and that credentials/connectivity are working before
# running the full pipeline.
#
# Usage:
#   LLM_BACKEND=ollama python -m ai.llm.test_backend
#   LLM_BACKEND=openai  OPENAI_API_KEY=sk-... python -m ai.llm.test_backend
#   LLM_BACKEND=gemini  GEMINI_API_KEY=...  python -m ai.llm.test_backend
#
# Expected output:
#   type: final
#   content: OK
#   tool_calls: []
# =============================================================================

import os
from ai.llm.factory import create_backend
from ai.llm.base import LLMRequest

if __name__ == "__main__":
    # The factory reads LLM_BACKEND (not LLM_PROVIDER). Default to "ollama"
    # if neither is set so this script works out of the box in a local dev env.
    if not os.getenv("LLM_BACKEND"):
        os.environ["LLM_BACKEND"] = "ollama"

    backend = create_backend()
    req = LLMRequest(
        messages=[
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Return exactly: OK"},
        ],
        tools=[],
        max_output_tokens=32,
        temperature=0.2,
    )
    resp = backend.step(req)
    print("type:", resp.type)
    print("content:", resp.content)
    print("tool_calls:", resp.tool_calls)
