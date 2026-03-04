# LN AI Network (Regtest) — Prompt-Driven Lightning Agent

This repo runs a **prompt-driven AI Lightning agent** on **regtest** that can autonomously complete workflows (e.g., end-to-end payments), while respecting a strict execution boundary:

**The agent may ONLY act via MCP tools** (no direct shell actions).

---

## What you get

- `ai/agent.py`: the controller/agent
  - Reads prompts from `runtime/agent/inbox.jsonl`
  - Writes reports to `runtime/agent/outbox.jsonl`
  - Logs a full per-prompt trace to `runtime/agent/trace.log` (**resets every new prompt**)
  - Enforces deterministic safety policies:
    - fail fast on tool errors
    - no redundant read-only tool calls unless state changed
    - oscillation detection
    - requires tools when goal unmet
    - deterministic tool-arg normalization (unwraps nested args + coerces ints)

- `mcp/ln_mcp_server.py`: the MCP tool boundary
  - Executes `bitcoin-cli` / `lightning-cli` operations
  - Exposes a tool surface the agent calls by name

- `scripts/restart_agent.sh`: clean restart + fresh mode

More detail:
- `docs/README.md`

---

## Quick Start

### 1) Start the agent (fresh run)
```bash
cd ~/lightning-network-ai-agents/ln-ai-network
scripts/restart_agent.sh fresh
tail -n 20 runtime/agent/agent.log
```

You should see an `agent_start` line with a `build` tag.

### 2) Send a prompt
You should have a shell helper function `ai_prompt`. Example:
```bash
ai_prompt "SMOKE TEST: call network_health and summarize."
```

Check output:
```bash
tail -n 5 runtime/agent/outbox.jsonl
```

---

## End-to-End Payment Test (Regtest)

Use this prompt to perform a full E2E payment flow.

```bash
ai_prompt 'END-TO-END PAYMENT TEST (REGTEST) — MUST COMPLETE OR REPORT EXACT BLOCKER

BOUNDARY:
- MCP tools only. No shell instructions.
- Fail fast: if any tool returns an error, STOP and report it.
- Nodes are 1-based: node=1 and node=2 only.

SUCCESS CRITERIA:
- node-1 and node-2 are running
- node-1 is connected to node-2 as a peer
- at least one confirmed/usable channel exists between node-1 and node-2
- node-2 creates an invoice for exactly 10,000 msat
- node-1 pays it using the exact payload.bolt11 string
- output strict JSON with e2e_ok=true

OUTPUT FORMAT (STRICT JSON ONLY):
Return ONE JSON object:
{
  "e2e_ok": boolean,
  "steps": [ {"name": string, "status": "ok"|"skip"|"fail", "details": object} ],
  "artifacts": {
    "node2_id": string|null,
    "node2_host": string|null,
    "node2_port": number|null,
    "funding": {"node1_addr": string|null, "node2_addr": string|null, "miner_addr": string|null},
    "invoice": {"bolt11": string|null, "msat": number|null, "label": string|null},
    "payment": {"status": string|null, "preimage": string|null}
  },
  "final_state": {
    "nodes_running": number,
    "node1_peers": number,
    "node1_channels": number,
    "node2_peers": number,
    "node2_channels": number
  },
  "tool_calls": [ {"tool": string, "args": object, "result_summary": string} ],
  "blocker": string|null
}

PLAN:
1) network_health
2) ensure node-2 running (ln_node_status, ln_node_start if needed)
3) ln_getinfo node=2 -> get id + binding
4) ln_connect from_node=1 -> node-2
5) fund both nodes (btc_wallet_ensure miner, ln_newaddr both, btc_sendtoaddress both, mine blocks)
6) open channel (ln_openchannel), mine confirmations until active
7) ln_invoice node=2 amount_msat=10000
8) ln_pay from_node=1 using EXACT invoice string payload.bolt11
9) verify + output strict JSON only'
```

---

## Logs & Debugging

### Outbox (agent reports)
```bash
tail -n 3 runtime/agent/outbox.jsonl
```

### Trace (full per-prompt, resets each prompt)
```bash
tail -n 120 runtime/agent/trace.log
```

### Agent runtime log
```bash
tail -n 120 runtime/agent/agent.log
```

### Save a trace run
```bash
mkdir -p runtime/agent/archive
cp runtime/agent/trace.log runtime/agent/archive/trace.$(date +%Y%m%d_%H%M%S).log
```

For deeper details on tools and errors, see:
- `docs/TOOLS.md`
- `docs/TROUBLESHOOTING.md`
