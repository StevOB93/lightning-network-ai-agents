#!/bin/bash
# shutdown.sh
# Stops agents, Lightning, and Bitcoin safely

echo "Stopping agents..."
pkill -f alternating_agent || true

sleep 2

echo "Stopping Core Lightning nodes..."
lightning-cli --rpc-file=$HOME/ln-ai-project/data/cln1/regtest/lightning-rpc stop || true
lightning-cli --rpc-file=$HOME/ln-ai-project/data/cln2/regtest/lightning-rpc stop || true

sleep 3

echo "Stopping Bitcoin Core..."
bitcoin-cli -regtest -datadir=$HOME/ln-ai-project/data/bitcoind stop || true

echo "Shutdown complete."
