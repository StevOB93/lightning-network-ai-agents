# MCP Server — Lightning Network Tool Interface

`mcp/ln_mcp_server.py` is the **Model Context Protocol server** that wraps every Bitcoin Core and Core Lightning operation as a validated, typed tool call.

The AI pipeline agent communicates exclusively through this server — it has no direct shell access, no direct RPC access, and no ability to run arbitrary commands. Every action the agent takes goes through one of the tools listed here.

---

## How it works

The MCP server is a long-running process started by `scripts/startup/0.2.control_plane_boot.sh`. The AI pipeline connects to it via stdio using `FastMCPClient`. On each tool call the server:

1. Validates required arguments are present
2. Resolves paths from env vars (`RUNTIME_DIR`, `LIGHTNING_BASE`, `BITCOIN_DIR`)
3. Runs the appropriate `bitcoin-cli` or `lightning-cli` subprocess
4. Parses the result, wraps it in a structured `{"ok": bool, "payload": ...}` envelope
5. Returns the envelope to the agent

All calls use a configurable timeout (`MCP_CMD_TIMEOUT_S`, default 30s).

---

## Tool reference

### System / health

| Tool | Required args | Description |
|------|--------------|-------------|
| `network_health` | — | Returns overall network status: node processes, channel count, Bitcoin block height, any connectivity issues. Use this first for diagnostics. |
| `sys_netinfo` | — | Returns this machine's hostname, default outbound IP, and all non-loopback interface IPs. Use before `ln_node_start` with `bind_host`/`announce_host` for cross-machine peer connectivity. |

---

### Bitcoin Core

| Tool | Required args | Optional args | Description |
|------|--------------|--------------|-------------|
| `btc_getblockchaininfo` | — | — | Returns Bitcoin node chain info: network, block height, sync status. |
| `btc_wallet_ensure` | `wallet_name` | — | Creates or loads a named Bitcoin wallet. Safe to call repeatedly — no-ops if the wallet already exists. |
| `btc_getnewaddress` | — | `wallet` | Generates a new Bitcoin address in the named wallet (defaults to the standard regtest wallet). |
| `btc_sendtoaddress` | `address`, `amount_btc` | `wallet` | Sends BTC from the named wallet to an address. `amount_btc` is a decimal string (e.g. `"0.001"`). |
| `btc_generatetoaddress` | `blocks`, `address` | — | Mines `blocks` regtest blocks, crediting the coinbase to `address`. Used to fund wallets and confirm transactions. |

---

### Lightning node lifecycle

| Tool | Required args | Optional args | Description |
|------|--------------|--------------|-------------|
| `ln_listnodes` | — | — | Lists all configured Lightning nodes and their running status. |
| `ln_node_status` | `node` | — | Returns detailed status for node N: process running, lightning-cli reachable, data directory present. |
| `ln_node_create` | `node` | — | Initialises the data directory and config file for node N. Does not start the process. |
| `ln_node_start` | `node` | `bind_host`, `announce_host` | Starts `lightningd` for node N. `bind_host` controls which interface the peer port binds on (default: `LN_BIND_HOST` env var). `announce_host` controls the address advertised to peers (default: same as bind). Pass `bind_host="0.0.0.0"` and `announce_host=<LAN-IP>` for cross-machine connectivity. |
| `ln_node_stop` | `node` | — | Stops `lightningd` for node N via `lightning-cli stop`. Waits for the process to exit. |
| `ln_node_delete` | `node` | — | Removes the data directory for node N. Node must be stopped first. **Irreversible.** |

---

### Lightning read operations

| Tool | Required args | Description |
|------|--------------|-------------|
| `ln_getinfo` | `node` | Returns node identity: pubkey, alias, color, block height, active peer count. Essential for connecting peers — the pubkey is the node's identity on the network. |
| `ln_listpeers` | `node` | Lists all connected peers for node N, including peer pubkeys, connection state, and associated channels. |
| `ln_listfunds` | `node` | Returns wallet balance (on-chain UTXOs and channel balances). Useful for confirming payment receipt or checking spendable liquidity. |
| `ln_listchannels` | `node` | Lists all channels known to node N: channel IDs, peer pubkeys, capacity, local/remote balance, and fee policies. |
| `ln_newaddr` | `node` | Returns a new on-chain address for node N's internal wallet. Used to fund the node before opening channels. |

---

### Lightning actions

| Tool | Required args | Description |
|------|--------------|-------------|
| `ln_connect` | `from_node`, `peer_id`, `host`, `port` | Connects node `from_node` to the peer at `host:port` with pubkey `peer_id`. Must succeed before opening a channel. `peer_id` is the full pubkey hex from `ln_getinfo`. |
| `ln_openchannel` | `from_node`, `peer_id`, `amount_sat` | Opens a payment channel from `from_node` to `peer_id` with capacity `amount_sat` satoshis. The funding node must have sufficient on-chain balance. |
| `ln_invoice` | `node`, `amount_msat`, `label`, `description` | Creates a BOLT11 payment request on node N for `amount_msat` millisatoshis. `label` must be unique per node. Returns the bolt11 string. |
| `ln_pay` | `from_node`, `bolt11` | Pays a BOLT11 invoice from node `from_node`. Returns payment preimage on success. |

---

## Result envelope

Every tool returns a JSON object with this shape:

```json
{
  "ok": true,
  "payload": { ... }
}
```

On error:
```json
{
  "ok": false,
  "error": "human-readable error message",
  "payload": null
}
```

The Executor reads `ok` to determine whether a step succeeded and uses `payload` for placeholder resolution (e.g. `$step1.result.payload.bolt11`).

---

## Configuration

The MCP server reads all configuration from environment variables set by `env.sh` (sourced at startup):

| Variable | Default | Description |
|----------|---------|-------------|
| `RUNTIME_DIR` | `<repo>/runtime` | Root of all runtime state |
| `BITCOIN_DIR` | `RUNTIME_DIR/bitcoin/shared` | Bitcoin Core data directory |
| `LIGHTNING_BASE` | `RUNTIME_DIR/lightning` | Parent directory for per-node Lightning data dirs |
| `BITCOIN_RPC_PORT` | `18443` | Bitcoin Core regtest RPC port |
| `BITCOIN_RPC_USER` | `lnrpc` | Bitcoin RPC username |
| `BITCOIN_RPC_PASSWORD` | `lnrpcpass` | Bitcoin RPC password |
| `LIGHTNING_BASE_PORT` | `9735` | Base peer port — node N listens on `9735 + N` |
| `LN_BIND_HOST` | `127.0.0.1` | Default bind address for Lightning peer ports |
| `LN_ANNOUNCE_HOST` | `LN_BIND_HOST` | Default announce address advertised to peers |
| `MCP_CMD_TIMEOUT_S` | `30` | Subprocess timeout for each bitcoin-cli / lightning-cli call |

---

## Running standalone (debugging)

```bash
# Start the MCP server and send a single tool call via stdin
echo '{"id":1,"method":"network_health","params":{}}' \
  | PYTHONPATH=. .venv/bin/python -m mcp.ln_mcp_server

# Check node 1 status
echo '{"id":1,"method":"ln_node_status","params":{"node":1}}' \
  | PYTHONPATH=. .venv/bin/python -m mcp.ln_mcp_server

# Get node 1 identity
echo '{"id":1,"method":"ln_getinfo","params":{"node":1}}' \
  | PYTHONPATH=. .venv/bin/python -m mcp.ln_mcp_server
```

In normal operation the MCP server is started by `scripts/startup/0.2.control_plane_boot.sh` and the AI pipeline connects to it automatically.
