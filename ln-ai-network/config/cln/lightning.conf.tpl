# lightning.conf.tpl
#
# This is a TEMPLATE, not a real config file.
# It is rendered once per node at runtime.
#
# All placeholders ({{LIKE_THIS}}) are replaced by start.sh.
# There must be exactly ONE template for all nodes.

# Bitcoin network mode (regtest/testnet/mainnet)
network={{BITCOIN_NETWORK}}

# Logging configuration
log-level=debug
log-file={{LOG_FILE}}

# Bitcoin Core RPC connection
bitcoin-rpcconnect=127.0.0.1
bitcoin-rpcport={{BITCOIN_RPC_PORT}}
bitcoin-rpcuser={{BITCOIN_RPC_USER}}
bitcoin-rpcpassword={{BITCOIN_RPC_PASSWORD}}

# Lightning network listening and announce addresses.
#
# bind-addr: the interface lightningd listens on for inbound peer connections.
#   Rendered from LN_BIND_HOST (default 127.0.0.1 = loopback only).
#   Set LN_BIND_HOST=0.0.0.0 in .env to accept connections from other machines.
#
# addr: the address advertised to the Lightning gossip network so remote peers
#   know where to reach this node.  Rendered from LN_ANNOUNCE_HOST, which defaults
#   to LN_BIND_HOST.  Override LN_ANNOUNCE_HOST with your public IP when behind NAT.
bind-addr={{LIGHTNING_BIND_HOST}}:{{LIGHTNING_PORT}}
addr={{LIGHTNING_ANNOUNCE_HOST}}:{{LIGHTNING_PORT}}

# Lightning RPC socket location
rpc-file={{RPC_SOCKET}}

# Security posture
# Deprecated APIs are disabled to reduce attack surface
allow-deprecated-apis=false
