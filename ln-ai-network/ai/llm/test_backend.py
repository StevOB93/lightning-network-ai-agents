import os
from ai.llm.factory import create_backend
from ai.llm.base import LLMRequest

if __name__ == "__main__":
    os.environ["LLM_PROVIDER"] = os.getenv("LLM_PROVIDER", "ollama")

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