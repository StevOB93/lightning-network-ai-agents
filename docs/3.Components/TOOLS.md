# MCP Tools Reference

The MCP server (`mcp/ln_mcp_server.py`) exposes 22 tools across 5 categories. All tools return `{"ok": bool, "payload": ...}`.

The AI agent calls these tools through `ai/mcp_client.py` over JSON-RPC on stdio. The agent has no other mechanism to interact with Bitcoin Core or Core Lightning.

## Tool conventions

**Node indexing is 1-based.** Use `node=1`, `node=2`, etc. Never `node=0`.

**Args must be top-level.** Pass arguments directly, not nested under a wrapper key:

```json
// Correct
{"node": 1}

// Wrong — do not wrap args
{"tool": "ln_getinfo", "args": {"node": "1"}}
```

The pipeline normalizes arguments automatically (unwraps nested args, coerces `"1"` → `1` for integer keys), but correct arg shape is still preferred.

---

## Category 1 — System / health

### `network_health()`

Quick health snapshot for Bitcoin Core and all known Lightning node directories.

Args: none

Returns: status of `bitcoind` and each `lightningd`, block height, node counts.

---

### `sys_netinfo()`

Returns this machine's hostname and non-loopback IP addresses. Used by the agent to determine which IP to announce when enabling cross-machine Lightning peer connectivity.

Args: none

Returns:

| Field | Description |
|-------|-------------|
| `hostname` | Machine hostname |
| `default_outbound_ip` | IP the OS would use to route outbound traffic |
| `all_ips` | All non-loopback IPv4 addresses |
| `ln_bind_host` | Current `LN_BIND_HOST` env var value |
| `ln_announce_host` | Current `LN_ANNOUNCE_HOST` env var value |

---

## Category 2 — Bitcoin Core

### `btc_getblockchaininfo()`

Args: none. Returns current blockchain state (chain, blocks, headers, etc.).

### `btc_wallet_ensure(wallet_name: string)`

Create or load a Bitcoin Core wallet. Always call with `wallet_name="miner"` before funding operations.

| Arg | Type | Description |
|-----|------|-------------|
| `wallet_name` | string | e.g. `"miner"` |

### `btc_getnewaddress(wallet?: string)`

Generate a new Bitcoin address in the given wallet.

| Arg | Type | Description |
|-----|------|-------------|
| `wallet` | string (optional) | Wallet name |

### `btc_sendtoaddress(address: string, amount_btc: string, wallet?: string)`

Send regtest BTC to an address.

| Arg | Type | Description |
|-----|------|-------------|
| `address` | string | Recipient address |
| `amount_btc` | string | Amount in BTC, e.g. `"1.0"` |
| `wallet` | string (optional) | Recommended: `"miner"` |

Common failure: error `-19 Wallet file not specified` → call `btc_wallet_ensure` first.

### `btc_generatetoaddress(blocks: int, address: string)`

Mine regtest blocks to an address.

| Arg | Type | Description |
|-----|------|-------------|
| `blocks` | int | Number of blocks to mine |
| `address` | string | Recipient (usually the miner address) |

---

## Category 3 — Node lifecycle

### `ln_listnodes()`

Args: none. Returns a list of all node directories known to the MCP server.

### `ln_node_status(node: int)`

Check whether a node's `lightningd` process is running.

| Arg | Type |
|-----|------|
| `node` | int |

### `ln_node_create(node: int)`

Create the data directory and config file for a new node without starting it.

| Arg | Type |
|-----|------|
| `node` | int |

### `ln_node_start(node: int, bind_host?: string, announce_host?: string)`

Start a `lightningd` node. Optionally bind to a specific interface and announce a routable address for cross-machine peer connections.

| Arg | Type | Description |
|-----|------|-------------|
| `node` | int | Node number (1-based) |
| `bind_host` | string (optional) | IP to bind on. Use `"0.0.0.0"` for all interfaces. Defaults to `LN_BIND_HOST` env var (`127.0.0.1`). |
| `announce_host` | string (optional) | IP/hostname to advertise to peers. Defaults to `LN_ANNOUNCE_HOST` env var. |

**Cross-machine connectivity:** call `sys_netinfo()` first to get the machine's outbound IP, then `ln_node_stop` + `ln_node_start(node, bind_host="0.0.0.0", announce_host=<detected_ip>)` to make the node reachable from another machine.

### `ln_node_stop(node: int)`

Stop a running `lightningd` node gracefully (via `lightning-cli stop`).

| Arg | Type |
|-----|------|
| `node` | int |

### `ln_node_delete(node: int)`

Remove a node's data directory entirely. The node must be stopped first.

| Arg | Type |
|-----|------|
| `node` | int |

---

## Category 4 — Lightning read

### `ln_getinfo(node: int)`

Get basic node information.

| Arg | Type |
|-----|------|
| `node` | int |

Key payload fields:
- `payload.id` — node pubkey (use in `ln_connect`, `ln_openchannel`)
- `payload.binding[0].address` — bound address
- `payload.binding[0].port` — bound port

### `ln_listpeers(node: int)`

List all connected peers.

| Arg | Type |
|-----|------|
| `node` | int |

### `ln_listfunds(node: int)`

List on-chain outputs and channel balances.

| Arg | Type |
|-----|------|
| `node` | int |

Channel states to look for: `"CHANNELD_NORMAL"` = active and usable.

### `ln_listchannels(node: int)`

List all channels visible to this node.

| Arg | Type |
|-----|------|
| `node` | int |

### `ln_newaddr(node: int)`

Generate a new on-chain address for the node's wallet. Address is in `payload.address` (may also be in `payload.bech32` depending on server version).

| Arg | Type |
|-----|------|
| `node` | int |

---

## Category 5 — Lightning actions

### `ln_connect(from_node: int, peer_id: string, host: string, port: int)`

Connect a node to a peer.

| Arg | Type | Description |
|-----|------|-------------|
| `from_node` | int | Source node |
| `peer_id` | string | Target node pubkey (from `ln_getinfo`) |
| `host` | string | Target node address |
| `port` | int | Target node port |

### `ln_openchannel(from_node: int, peer_id: string, amount_sat: int)`

Open a payment channel. Requires the target peer to be connected and both nodes to have on-chain funds. Mine 6 blocks after calling to confirm.

| Arg | Type | Description |
|-----|------|-------------|
| `from_node` | int | Source node |
| `peer_id` | string | Target node pubkey |
| `amount_sat` | int | Channel capacity in satoshis |

### `ln_invoice(node: int, amount_msat: int, label: string, description: string)`

Create a BOLT11 invoice.

| Arg | Type | Description |
|-----|------|-------------|
| `node` | int | Payee node |
| `amount_msat` | int | Amount in millisatoshis |
| `label` | string | Unique invoice label |
| `description` | string | Human-readable description |

Returns invoice string in `payload.bolt11`.

### `ln_pay(from_node: int, bolt11: string)`

Pay a BOLT11 invoice.

| Arg | Type | Description |
|-----|------|-------------|
| `from_node` | int | Payer node |
| `bolt11` | string | **Exact** invoice string from `ln_invoice` |

Returns payment preimage on success.

---

## Golden path: end-to-end payment

Typical tool call order for a complete payment:

1. `network_health` — verify infrastructure
2. `ln_node_status(node=2)` → `ln_node_start(node=2)` if needed
3. `ln_getinfo(node=2)` → get `id` and `binding`
4. `ln_connect(from_node=1, peer_id=..., host=..., port=...)`
5. `btc_wallet_ensure(wallet_name="miner")`
6. `ln_newaddr(node=1)` and `ln_newaddr(node=2)` → get addresses
7. `btc_sendtoaddress(...)` to both nodes + `btc_generatetoaddress(blocks=101, ...)` to confirm
8. `ln_openchannel(from_node=1, peer_id=..., amount_sat=500000)` + `btc_generatetoaddress(blocks=6, ...)`
9. `ln_invoice(node=2, amount_msat=10000, label="...", description="...")`
10. `ln_pay(from_node=1, bolt11=...)` — use the **exact** `payload.bolt11` string
11. `ln_listfunds(node=2)` — verify balance increased
