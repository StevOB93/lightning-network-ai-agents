# Quickstart — LN AI Network (Regtest)

This quickstart covers **installation + basic setup** for the prompt-driven Lightning Network agent on **regtest**, including how to start the infrastructure, start the agent, and run a smoke test / E2E payment test.

> Core rule: the LLM agent **acts ONLY via MCP tools**. The MCP server is the execution boundary.

---

## Prereqs

- Linux or WSL2 Ubuntu (recommended; your paths assume `$HOME/lightning-network-ai-agents/...`)
- Python 3.10+ (3.10 is fine)
- `git`
- A working local **Ollama** install if using `LLM_BACKEND=ollama`

---

## 1) Clone the repo

```bash
mkdir -p ~/lightning-network-ai-agents
cd ~/lightning-network-ai-agents
git clone <YOUR_REPO_URL> ln-ai-network
cd ln-ai-network
```

---

## 2) Python virtual environment

If the repo already contains `.venv/`, you can skip creation and just install deps.

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
```

Install requirements (pick the one your repo uses):

```bash
# If you have requirements.txt
./.venv/bin/pip install -r requirements.txt

# Or if you have pyproject.toml (poetry/pdm/etc), use the tool your repo uses.
```

---

## 3) Regtest infrastructure (Bitcoin + Lightning)

Your project already has scripts to start/stop the regtest stack. Use the repo scripts you’ve been using for infra boot.

Typical pattern:

```bash
# Start infra (bitcoind + node-1 at minimum)
# (Use the script your repo provides; examples shown)
scripts/start.sh
# or:
scripts/boot_infra.sh
```

Confirm basic health (optional, if you have helper scripts):
```bash
scripts/network_health.sh || true
```

> In your current architecture: infra boot starts **node-1** only; the AI agent can start **node-2+** via MCP tools when needed.

---

## 4) Start the agent (recommended: fresh)

The agent controller:
- reads `runtime/agent/inbox.jsonl`
- writes `runtime/agent/outbox.jsonl`
- writes a per-prompt trace to `runtime/agent/trace.log` (resets each prompt)

Start it with the provided restart script:

```bash
scripts/restart_agent.sh fresh
tail -n 30 runtime/agent/agent.log
```

You should see an `agent_start` log line with a `build` tag.

### Environment variables (most important)

These are the usual ones you’ll care about:

```bash
export ALLOW_LLM=1
export LLM_BACKEND=ollama        # or openai, etc (depending on your factory.py)
export LLM_TEMPERATURE=0         # recommended for determinism
export LLM_MAX_STEPS_PER_COMMAND=60
```

If using Ollama:
```bash
export OLLAMA_BASE_URL=http://127.0.0.1:11434
export OLLAMA_MODEL=llama3.2:3b  # adjust to the model you pulled
export OLLAMA_TOOL_TEMP_ZERO=1   # keep tool-driven runs stable
```

---

## 5) Install the `ai_prompt` helper (bash)

If you don’t already have it in `.bashrc`, you can add this function:

```bash
ai_prompt () {
  local prompt="$*"
  if [[ -z "$prompt" ]]; then
    echo "Usage: ai_prompt \"your prompt here\""
    return 2
  fi

  local root="$HOME/lightning-network-ai-agents/ln-ai-network"
  local inbox="$root/runtime/agent/inbox.jsonl"
  mkdir -p "$(dirname "$inbox")"

  "$root/.venv/bin/python" - <<PY
import json, time
from pathlib import Path
inbox = Path(r"$inbox")
msg = {"id": int(time.time()), "content": r"""$prompt""", "meta": {"kind":"freeform","use_llm": True}}
with inbox.open("a", encoding="utf-8") as f:
    f.write(json.dumps(msg, ensure_ascii=False) + "\\n")
print("Queued prompt id:", msg["id"])
PY
}
```

Reload shell:
```bash
source ~/.bashrc
```

---

## 6) Smoke test

Send a minimal prompt:

```bash
ai_prompt "SMOKE TEST: call network_health once and summarize state."
sleep 0.5
tail -n 3 runtime/agent/outbox.jsonl
```

If you want deep debug detail for the same run:
```bash
tail -n 120 runtime/agent/trace.log
```

---

## 7) End-to-end payment test (regtest)

This is the “golden path” test you’ll run repeatedly while iterating:

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
    "payment": {"status": string|null, "preimage": string|null }
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

Check results:
```bash
tail -n 1 runtime/agent/outbox.jsonl
tail -n 120 runtime/agent/trace.log
```

---

## 8) Saving trace logs

Trace resets every prompt by design. If you want to keep a run:

```bash
mkdir -p runtime/agent/archive
cp runtime/agent/trace.log runtime/agent/archive/trace.$(date +%Y%m%d_%H%M%S).log
```

---

## 9) If something fails: what to paste into a debugging chat

Provide:
```bash
tail -n 1 runtime/agent/outbox.jsonl
tail -n 160 runtime/agent/trace.log
tail -n 120 runtime/agent/agent.log
```

Common failure categories:
- Bitcoin wallet selection (`-19 Wallet file not specified`)
- node RPC not ready (`lightning-rpc: Connection refused`)
- no peers/channels (needs connect + open channel + mine confirmations)
- malformed tool args (agent now normalizes, but trace will show `tool_args_normalized` / `tool_args_invalid`)

---

## Notes on determinism

For best results in tool-driven automation:
- set `LLM_TEMPERATURE=0`
- keep Ollama tool temperature zeroing enabled (`OLLAMA_TOOL_TEMP_ZERO=1`)
- rely on trace logs for step-by-step postmortems
