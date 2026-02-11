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
