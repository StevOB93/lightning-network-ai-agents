# Person A — AI / MCP / Control Plane (Read-Only)

This subsystem **does not** run Lightning, Bitcoin, shell scripts, or lightning-cli.

## Guarantees
- AI reads via MCP tools only
- AI outputs structured intent JSON only
- AI never executes intents
- Tool failures are meaningful signals → AI emits `noop`

## Mock Mode (offline development)
Fixtures emulate MCP tool outputs deterministically.

Run agent on a fixture:
```bash
python -m ai.agent
```

## Gemini setup

The default provider is `gemini`, using Google's OpenAI-compatible API.

Minimal setup:
```bash
cp .env.example .env
# set GEMINI_API_KEY in .env
source env.sh
pip install -r requirements.txt
```

Run the agent:
```bash
python -m ai.agent
```

Queue simple requests from another terminal:
```bash
python -m ai.cli health
python -m ai.cli last

python -m ai.cli ask --llm "Run a network health check and summarize what is working."
python -m ai.cli last
```
