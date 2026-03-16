# MCP Tools — Current Surface (Regtest)

This doc describes the tool surface the agent can call. These tools are implemented by `mcp/ln_mcp_server.py` and called by name over MCP.

## Global Rules

### 1) Tool calls must pass ONLY required args at top level
✅ Good:
```json
{"node": 1}
```

❌ Bad (wrapper objects / nested args):
```json
{"tool":"ln_listpeers","args":{"node":"1"},"status":"fail","result":{...}}
```

The current agent build includes deterministic arg normalization:
- unwraps nested `"args": {...}` into top-level args
- coerces integer-like strings -> ints for common numeric keys
- validates required keys before calling MCP

### 2) Node indexing is 1-based
Use `node=1`, `node=2`, etc. No `node=0`.

---

## Health

### `network_health()`
Purpose:
- Quick health snapshot for bitcoind + all known Lightning node dirs.

Args: none

---

## Bitcoin (Regtest)

### `btc_getblockchaininfo()`
Args: none

### `btc_wallet_ensure(wallet_name: string)`
Args:
- `wallet_name` (string) — e.g., `"miner"`

### `btc_getnewaddress(wallet?: string)`
Args:
- `wallet` (optional string)

### `btc_sendtoaddress(address: string, amount_btc: string, wallet?: string)`
Args:
- `address` (string)
- `amount_btc` (string) e.g. `"1.0"`
- `wallet` (optional string, recommended: `"miner"`)

Common failure:
- Bitcoin Core error `-19 Wallet file not specified` → ensure wallet is specified/loaded.

### `btc_generatetoaddress(blocks: int, address: string)`
Args:
- `blocks` (int)
- `address` (string) — usually miner address

---

## Lightning Node Lifecycle

### `ln_listnodes()`
Args: none

### `ln_node_status(node: int)`
Args:
- `node` (int)

### `ln_node_start(node: int)`
Args:
- `node` (int)

---

## Lightning Read Tools

### `ln_getinfo(node: int)`
Args:
- `node` (int)

Important fields:
- `payload.id` (node pubkey)
- `payload.binding[0].address`
- `payload.binding[0].port`

### `ln_listpeers(node: int)`
Args:
- `node` (int)

### `ln_listfunds(node: int)`
Args:
- `node` (int)

### `ln_listchannels(node: int)`
Args:
- `node` (int)

### `ln_newaddr(node: int)`
Args:
- `node` (int)

Note:
- address may be in `payload.address` (preferred) or `payload.bech32` depending on server version.

---

## Lightning Action Tools

### `ln_connect(from_node: int, peer_id: string, host: string, port: int)`
Args:
- `from_node` (int)
- `peer_id` (string)
- `host` (string)
- `port` (int)

### `ln_openchannel(from_node: int, peer_id: string, amount_sat: int)`
Args:
- `from_node` (int)
- `peer_id` (string)
- `amount_sat` (int)

---

## Payments

### `ln_invoice(node: int, amount_msat: int, label: string, description: string)`
Args:
- `node` (int)
- `amount_msat` (int)
- `label` (string) unique
- `description` (string)

Returns invoice string in `payload.bolt11`.

### `ln_pay(from_node: int, bolt11: string)`
Args:
- `from_node` (int)
- `bolt11` (string) EXACT invoice

---

## Expected “Golden Path” Order (E2E)
1) `network_health`
2) `ln_node_status node=2`, `ln_node_start node=2` if needed
3) `ln_getinfo node=2` -> id/binding
4) `ln_connect from_node=1`
5) `btc_wallet_ensure miner`
6) `ln_newaddr` for node-1 and node-2
7) `btc_sendtoaddress` to both addresses, `btc_generatetoaddress` mine confirmations
8) `ln_openchannel` + mine confirmations
9) `ln_invoice` + `ln_pay`
10) verify (`ln_listfunds`, `ln_listchannels`, `network_health`)
