# Documentation Index

- **Tools**: `TOOLS.md` — MCP tool catalog, arguments, expected usage.
- **Troubleshooting**: `TROUBLESHOOTING.md` — common failures, how to collect logs, fixes.

## Recommended workflow

1) Start agent:
   - `scripts/restart_agent.sh fresh`

2) Run the E2E payment prompt (see docs/quickstart/ `quickstart.md`)

3) If it fails, collect:
   - `tail -n 1 runtime/agent/outbox.jsonl`
   - `tail -n 120 runtime/agent/trace.log`
   - `tail -n 120 runtime/agent/agent.log`
