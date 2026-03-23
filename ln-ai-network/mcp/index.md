# MCP — Model Context Protocol Server

See [README.md](README.md) for the full tool reference, configuration, and usage guide.

## Quick reference

The MCP server (`mcp/ln_mcp_server.py`) exposes 22 tools across 5 categories:

| Category | Tools |
|----------|-------|
| System / health | `network_health`, `sys_netinfo` |
| Bitcoin Core | `btc_getblockchaininfo`, `btc_wallet_ensure`, `btc_getnewaddress`, `btc_sendtoaddress`, `btc_generatetoaddress` |
| Node lifecycle | `ln_listnodes`, `ln_node_status`, `ln_node_create`, `ln_node_start`, `ln_node_stop`, `ln_node_delete` |
| Lightning read | `ln_getinfo`, `ln_listpeers`, `ln_listfunds`, `ln_listchannels`, `ln_newaddr` |
| Lightning actions | `ln_connect`, `ln_openchannel`, `ln_invoice`, `ln_pay` |

All tools return `{"ok": bool, "payload": ...}`.
