# Troubleshooting

When something goes wrong, start here. Collect the three diagnostic files:

```bash
tail -n 1  ln-ai-network/runtime/agent/outbox.jsonl
tail -n 120 ln-ai-network/runtime/agent/trace.log
tail -n 120 ln-ai-network/logs/system/0.3.agent_boot.log
```

Or use the **Crash Kit** button in the web UI — it copies a formatted debug snapshot to your clipboard.

---

## Common failures

### 1. "OPENAI_API_KEY not set" / LLM credentials error

The agent validates LLM credentials at startup and fails fast if they are missing or still set to the placeholder value.

**Fix:**
```bash
cp ln-ai-network/.env.example ln-ai-network/.env
# Edit .env and set a real API key + ALLOW_LLM=1
./start.sh
```

---

### 2. Web UI doesn't open automatically

Navigate manually to `http://127.0.0.1:8008`. On WSL2, ensure your Windows browser can reach `localhost` (it should by default on WSL2).

**Also check:** `tail -n 50 ln-ai-network/logs/system/0.4.ui_server.log`

---

### 3. Agent not responding to prompts

The agent may not be running, or the lock file may be stale.

**Check:**
```bash
cat ln-ai-network/runtime/agent/pipeline.lock    # should show pid=NNNN
ps aux | grep "python -m ai.pipeline" | grep -v grep
tail -n 50 ln-ai-network/logs/system/0.3.agent_boot.log
```

**Fix:**
```bash
cd ln-ai-network
./scripts/restart_agent.sh fresh
```

---

### 4. "exceeded max steps" — agent loops without completing

The LLM is not making progress or is refusing to call tools.

**Check `trace.log` for:**
- Repeated `resp_type: final` after `forced_tools_injected`
- Refusal messages or oscillation

**Fix:**
- Set `LLM_TEMPERATURE=0` in `.env`
- Restart the agent: `./scripts/restart_agent.sh`

---

### 5. Tool error: "Missing required param: 'node'"

The LLM called a tool with a malformed argument shape (e.g., wrapped under `"args": {...}`).

The pipeline includes deterministic argument normalization — it unwraps nested args, coerces `"1"` → `1`, and validates required keys before the MCP call. If this still fails, check `trace.log` for `tool_args_normalized` or `tool_args_invalid` events.

---

### 6. Bitcoin Core error -19 "Wallet file not specified"

```
error code: -19
error message: Wallet file not specified
```

**Fix:** Ensure the `miner` wallet is loaded. The MCP tool `btc_wallet_ensure` handles this:
> "Make sure the miner wallet exists"

Or call it directly:
```bash
bitcoin-cli -regtest -rpcport=18443 -rpcuser=lnrpc -rpcpassword=lnrpcpass loadwallet miner
```

---

### 7. "Connection refused" — lightning-rpc

A Lightning node is not running or its RPC socket is not ready.

**Fix:**
1. Check node status: ask the agent "what is the status of node 2?"
   - The agent will call `ln_node_status(node=2)` and `ln_node_start(node=2)` if needed
2. Or check directly:
   ```bash
   lightning-cli --lightning-dir=ln-ai-network/runtime/lightning/node-2 --network=regtest getinfo
   ```

---

### 8. No peers / no route / payment fails

Nodes are not connected, or no confirmed channel exists.

**Fix path** (ask the agent or step through manually):
1. `ln_getinfo(node=2)` → get pubkey and binding address
2. `ln_connect(from_node=1, peer_id=..., host=..., port=...)` → connect peers
3. `ln_listpeers(node=1)` → verify peer connected
4. Fund both nodes with on-chain BTC and mine 101 blocks
5. `ln_openchannel(...)` → open channel, mine 6 blocks to confirm
6. Verify with `ln_listchannels(node=1)`

---

### 9. Channel pending / not active

The channel funding transaction has not confirmed yet.

**Fix:**
```bash
# Ask the agent: "mine 6 more blocks"
# Or call btc_generatetoaddress directly via the agent:
# "Generate 6 blocks to the miner address"
```

Check status: `ln_listfunds(node=1)` — look for `"state": "CHANNELD_NORMAL"`.

---

### 10. bitcoind / lightningd not found

```bash
./install.sh
```

Run the one-time installer to download and install the required binaries.

---

### 11. Port conflicts

If another process is using a port:

```bash
# Override in ln-ai-network/.env:
BITCOIN_RPC_PORT=18443
BITCOIN_P2P_PORT=18444
LIGHTNING_BASE_PORT=9735
UI_PORT=8008
```

Then restart: `./stop.sh && ./start.sh`

---

## Saving a trace for debugging

Trace logs reset on every prompt. Save a run before it gets overwritten:

```bash
mkdir -p ln-ai-network/runtime/agent/archive
cp ln-ai-network/runtime/agent/trace.log \
   ln-ai-network/runtime/agent/archive/trace.$(date +%Y%m%d_%H%M%S).log
```

The Logs tab in the web UI also has an **Archive** panel showing past pipeline runs automatically saved to `logs/pipeline/`.

---

## Collecting a bug report

Provide these three outputs:

```bash
tail -n 1   ln-ai-network/runtime/agent/outbox.jsonl
tail -n 160 ln-ai-network/runtime/agent/trace.log
tail -n 120 ln-ai-network/logs/system/0.3.agent_boot.log
```

Or use the **Copy Crash Kit** button in the web UI (Logs tab) — it formats all of this automatically.
