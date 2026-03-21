# Starting the system — `1.start.sh`

`scripts/1.start.sh` is the **full system launcher**. It starts every component in the correct order — infrastructure, control plane, AI agent, and web UI — and streams live logs until everything is running.

---

## Usage

```bash
# Start with 2 Lightning nodes (default)
./scripts/1.start.sh

# Start with 3 Lightning nodes
./scripts/1.start.sh 3

# Force reinstall Python dependencies first
REINSTALL_PY_DEPS=1 ./scripts/1.start.sh 2
```

`NODE_COUNT` is saved to `runtime/node_count` so that `shutdown.sh` can stop the correct number of nodes without requiring the argument again.

---

## Prerequisites

- `scripts/0.install.sh` has been run at least once
- `bitcoind` and `lightningd` are on `PATH`
- `.env` exists with valid LLM credentials (copy from `.env.example`)
- No other instance of the system is already running

---

## Startup sequence

`1.start.sh` runs four sub-scripts in order, streaming each step's output to the console and to `logs/system/`:

### Step 1 — Infrastructure boot (`0.1.infra_boot.sh`)

**Log:** `logs/system/0.1.infra_boot.log`

1. Creates `runtime/bitcoin/shared/` and `runtime/lightning/node-N/` directories
2. Writes `bitcoin.conf` for the shared Bitcoin Core instance (regtest, RPC credentials, P2P port)
3. Starts `bitcoind` in daemon mode
4. Waits for the Bitcoin RPC to become responsive
5. Creates or loads the default Bitcoin wallet
6. For each Lightning node N (1 through NODE_COUNT):
   - Writes `config` for `lightningd` (network, ports, Bitcoin connection details)
   - Starts `lightningd` in the background
   - Waits for `lightning-cli getinfo` to succeed
7. Connects each node to every other node (full mesh for regtest convenience)
8. Funds each node's on-chain wallet with regtest coins
9. Mines enough blocks to make the funds spendable (coinbase maturity: 101 blocks)
10. Opens a channel between node 1 and node 2 (and any additional nodes), mines 6 blocks to confirm

### Step 2 — Control plane boot (`0.2.control_plane_boot.sh`)

**Log:** `logs/system/0.2.control_plane_boot.log`

Starts the MCP server (`mcp/ln_mcp_server.py`) as a background process and writes its PID to `runtime/mcp.pid`. The MCP server provides the AI agent with all Bitcoin and Lightning tool calls.

### Step 3 — Agent boot (`0.3.agent_boot.sh`)

**Log:** `logs/system/0.3.agent_boot.log`

Starts the AI pipeline (`ai/pipeline.py`) as a background process and writes its PID to `runtime/agent/agent.pid` and `runtime/agent/pipeline.lock`. The agent begins polling `runtime/agent/inbox.jsonl` for new commands immediately.

Validates LLM credentials before launching (fails early with a clear error if the API key is missing or still a placeholder).

### Step 4 — Web UI (`0.4.ui_server.sh`)

**Log:** `logs/system/0.4.ui_server.log`

Starts the HTTP + SSE web server (`scripts/ui_server.py`) on `UI_HOST:UI_PORT` (default `127.0.0.1:8008`) and writes its PID to `runtime/ui_server.pid`. On Linux, automatically opens the browser to the UI URL.

---

## Runtime layout after start

```
runtime/
  node_count              # Written by start.sh: "2" (or whatever NODE_COUNT was)
  mcp.pid                 # PID of the MCP server process
  ui_server.pid           # PID of the web UI server process
  bitcoin/
    shared/               # bitcoind data directory
      bitcoin.conf
      regtest/
  lightning/
    node-1/               # lightningd data for node 1
      config
      hsm_secret
      regtest/
    node-2/               # lightningd data for node 2
      ...
  agent/
    agent.pid             # PID of the pipeline process
    pipeline.lock         # Single-instance lock
    inbox.jsonl           # Incoming commands
    outbox.jsonl          # Pipeline results
    inbox.offset          # Read cursor for inbox
    history.jsonl         # Conversation history (last N turns)
    trace.log             # Live pipeline trace events (JSON lines)
    stream.jsonl          # LLM token stream for the UI
logs/
  system/
    start.log             # Top-level start log
    0.1.infra_boot.log
    0.2.control_plane_boot.log
    0.3.agent_boot.log
    0.4.ui_server.log
```

---

## Signal handling

If `1.start.sh` is interrupted (Ctrl+C or SIGTERM) while a boot step is running, it kills the current step's subprocess (and any `tail -f` follower) without touching the already-running Bitcoin or Lightning processes. This prevents orphaned step processes from holding ports or lock files that would block a subsequent start.

---

## Stopping the system

```bash
# Stop everything (reads NODE_COUNT from runtime/node_count automatically)
./scripts/shutdown.sh

# Or specify explicitly
./scripts/shutdown.sh 2
```

Shutdown runs in reverse order:
1. Stops the AI agent (sends SIGTERM, removes lock)
2. Stops the MCP server
3. Stops each `lightningd` via `lightning-cli stop`, then `bitcoind` via `bitcoin-cli stop`
4. Kills the web UI server (reads `runtime/ui_server.pid`)

The **Restart** and **Shutdown** buttons in the web UI trigger `1.start.sh` / `shutdown.sh` respectively via the `/api/restart` and `/api/shutdown` endpoints.

---

## Restarting the agent only

If you change AI pipeline code and want to reload it without restarting the Bitcoin/Lightning infrastructure:

```bash
# Restart agent, keep inbox/outbox
./scripts/restart_agent.sh

# Restart agent and clear queue state
./scripts/restart_agent.sh fresh
```

---

## Environment variables

All variables are set by `env.sh` (sourced at startup) and can be overridden in `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `BITCOIN_RPC_PORT` | `18443` | Bitcoin Core regtest RPC port |
| `BITCOIN_P2P_PORT` | `18444` | Bitcoin Core P2P port |
| `LIGHTNING_BASE_PORT` | `9735` | Lightning peer base port (node N uses `9735 + N`) |
| `BITCOIN_RPC_USER` | `lnrpc` | Bitcoin RPC username |
| `BITCOIN_RPC_PASSWORD` | `lnrpcpass` | Bitcoin RPC password |
| `LN_BIND_HOST` | `127.0.0.1` | Interface `lightningd` binds peer ports on |
| `LN_ANNOUNCE_HOST` | `LN_BIND_HOST` | Address advertised to Lightning gossip peers |
| `LLM_BACKEND` | `openai` | AI provider: `openai`, `ollama`, `gemini` |
| `OPENAI_API_KEY` | — | Required for OpenAI |
| `GEMINI_API_KEY` | — | Required for Gemini |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Ollama endpoint |
| `UI_HOST` | `127.0.0.1` | Web UI bind address |
| `UI_PORT` | `8008` | Web UI port |
| `REINSTALL_PY_DEPS` | `0` | Set to `1` to force `pip install -r requirements.txt` on start |

---

## Cross-machine Lightning peers

To make nodes reachable from another machine on the same network:

```bash
# In .env on each machine:
LN_BIND_HOST=0.0.0.0
LN_ANNOUNCE_HOST=192.168.1.10   # this machine's LAN IP
```

Or let the AI agent configure this autonomously:
> "Make node 1 reachable from another machine"

The agent will call `sys_netinfo` to find the local IP, then restart the node with the correct bind and announce addresses.
