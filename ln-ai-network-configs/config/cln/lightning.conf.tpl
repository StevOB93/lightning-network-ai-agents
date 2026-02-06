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

# Lightning network listening address
# Port is dynamically assigned per node
addr=127.0.0.1:{{LIGHTNING_PORT}}
bind-addr=127.0.0.1:{{LIGHTNING_PORT}}

# Lightning RPC socket location
rpc-file={{RPC_SOCKET}}

# Security posture
# Deprecated APIs are disabled to reduce attack surface
allow-deprecated-apis=false
