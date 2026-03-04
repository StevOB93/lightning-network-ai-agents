# Troubleshooting — LN AI Network (Regtest)

When a run fails, collect:

```bash
tail -n 1 runtime/agent/outbox.jsonl
tail -n 120 runtime/agent/trace.log
tail -n 120 runtime/agent/agent.log
```

## Common Failures & Fixes

## 1) “ERROR: exceeded max steps”
Meaning:
- The LLM did not progress using tools (looping in “final” responses) OR is stuck.

What to check:
- `runtime/agent/trace.log`:
  - repeated `resp_type: final` after “forced_tools_injected”
  - refusal / oscillation / max_steps stop reasons

Fixes:
- Lower temperature (`LLM_TEMPERATURE=0`, `OLLAMA_TOOL_TEMP_ZERO=1` if supported).
- Ensure tool calling is supported/hardened in the backend.
- Confirm tool schema is correct in `ai/agent.py`.

---

## 2) Tool error: “Missing required param: 'node'”
Meaning:
- LLM called tool with wrong arg shape (nested under `"args": {...}`) or included wrapper fields.

Fix:
- Current agent build normalizes args deterministically:
  - unwraps nested args
  - coerces `"1"` -> `1`
  - validates required keys before MCP call

Check trace for:
- `tool_args_normalized`
- `tool_args_invalid`

---

## 3) Bitcoin Core error -19 “Wallet file not specified”
Meaning:
- `bitcoin-cli` requires selecting a wallet via `-rpcwallet=<wallet>` or `/wallet/<name>`.

Fix:
- Ensure:
  - `btc_wallet_ensure wallet_name="miner"`
  - `btc_sendtoaddress` uses `wallet="miner"` (or MCP server defaults)

---

## 4) “Connection refused” lightning-rpc
Meaning:
- lightningd isn’t running, or RPC file not ready.

Fix:
- `ln_node_status`
- `ln_node_start`
- retry `ln_getinfo` after node is fully up

---

## 5) No peers / no route / cannot pay
Meaning:
- Nodes are not connected OR channel doesn’t exist/confirm.

Fix path:
1) `ln_getinfo node=2` -> get id + binding
2) `ln_connect from_node=1 ...`
3) verify `ln_listpeers node=1`
4) fund nodes + mine
5) open channel + mine

---

## 6) Channel pending / not active
Meaning:
- channel funding tx not confirmed yet.

Fix:
- Mine blocks: `btc_generatetoaddress blocks=6 address=<miner_addr>`
- Recheck: `ln_listfunds` and/or `ln_listchannels`
- Mine additional batches if needed

---

## 7) Agent not producing output
Meaning:
- agent not running, lock stuck, or inbox offset wedged.

Fix:
```bash
scripts/restart_agent.sh fresh
tail -n 50 runtime/agent/agent.log
```

Check:
- lock file: `runtime/agent/agent.lock`
- process:
  ```bash
  ps aux | grep -E "python -m ai\.agent" | grep -v grep
  ```

---

## 8) Save a trace run
Trace resets every prompt by design. Save it:

```bash
mkdir -p runtime/agent/archive
cp runtime/agent/trace.log runtime/agent/archive/trace.$(date +%Y%m%d_%H%M%S).log
```
